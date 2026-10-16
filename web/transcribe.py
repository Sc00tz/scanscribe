"""ScanScribe transcription worker: faster-whisper on CPU. Transcribes new calls, newest first."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tones  # noqa: E402
from app import CAPTURE_DIR, connect, init_db  # noqa: E402

TONES_ENABLED = os.environ.get("TONE_DETECTION", "on").strip().lower() not in ("off", "0", "false", "no")
MODEL = os.environ.get("WHISPER_MODEL", "small.en")
THREADS = int(os.environ.get("WHISPER_THREADS", "2"))
LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "en")
PROMPT = os.environ.get("WHISPER_PROMPT", "Police, fire and EMS radio dispatch in Concord, New Hampshire.")
MAX_ATTEMPTS = 3
# Calls longer than this are never sent to whisper (a stuck-open squelch can make multi-hour "calls")
MAX_CALL_SECONDS = int(os.environ.get("WHISPER_MAX_CALL_SECONDS", "300"))


def build_prompt():
    """Base prompt plus local place names and unit types so whisper spells them right.
    Whisper only keeps the last ~220 tokens of the prompt, so the hints are kept short."""
    base = PROMPT
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "codes.json"), encoding="utf-8") as f:
            data = json.load(f)
        places = [n for _, n in data["communities"]
                  if not any(w in n for w in ("Team", "Academy", "Rescue", "Ambulance"))]
        base += (" Towns: " + ", ".join(places) + ". Units: Engine, Ladder, Tower, Rescue, Tanker,"
                 " Forestry, Ambulance, Tri-Town Ambulance, Penacook Rescue, Chief, Capital Area.")
    except (OSError, ValueError, KeyError):
        pass
    return base


def load_model():
    from faster_whisper import WhisperModel
    print(f"loading whisper model {MODEL} (cpu int8, {THREADS} threads)", flush=True)
    return WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=THREADS)


def split_turns(words, src_times):
    """Group words into [{src, text}] using when each radio started transmitting.

    words: [(start_seconds, text)]; src_times: [[radio_id, start_seconds], ...] from the call JSON.
    Returns [] unless at least two different radios spoke."""
    marks = sorted((pos, u) for u, pos in src_times)
    if len({u for _, u in marks}) < 2:
        return []
    turns = []
    for start, text in words:
        who = marks[0][1]
        for pos, u in marks:
            if pos - 0.25 <= start:  # small tolerance: the decoder timestamp lags the first word
                who = u
        if turns and turns[-1]["src"] == who:
            turns[-1]["text"] += text
        else:
            turns.append({"src": who, "text": text})
    for t in turns:
        t["text"] = t["text"].strip()
    return [t for t in turns if t["text"]]


def scan_tones(call_id, audio_path, length_ms):
    """Look for paging tones in one call and store them. Never raises: a tone problem must not stop
    transcription. Calls longer than the transcription limit (stuck-open channels) are marked scanned
    without being read. Returns the events found."""
    events, status = [], 1
    if TONES_ENABLED and (length_ms or 0) <= MAX_CALL_SECONDS * 1000:
        try:
            events = tones.detect(CAPTURE_DIR / audio_path)
        except Exception as exc:
            status = 2
            print(f"call {call_id}: tone scan failed: {exc}", flush=True)
    with connect() as con:
        con.execute("DELETE FROM tone_events WHERE call_id=?", (call_id,))
        for e in events:
            con.execute("INSERT INTO tone_events (call_id, freq_a, freq_b, a_len, b_len, start_s, end_s) VALUES (?,?,?,?,?,?,?)",
                        (call_id, e["freq_a"], e["freq_b"], e["a_len"], e["b_len"], e["start"], e["end"]))
        con.execute("UPDATE calls SET tones_checked=? WHERE id=?", (status, call_id))
    if events:
        pairs = ", ".join(f"{e['freq_a']:.0f}/{e['freq_b']:.0f} Hz" for e in events)
        print(f"call {call_id}: paging tone {pairs}", flush=True)
    return events


def stored_tone_end(call_id):
    """Seconds into the call where its last paging tone ends (0 if none). Only tones that start near the
    beginning count, so a stray detection later in a call can never hide real speech."""
    with connect() as con:
        row = con.execute("SELECT MAX(end_s) FROM tone_events WHERE call_id=? AND start_s<=?",
                          (call_id, tones.SKIP_WINDOW_S)).fetchone()
    return float(row[0] or 0)


def transcribe(model, path, src_times, prompt=None, skip_s=0.0):
    """Transcribe a call, ignoring the first skip_s seconds (the paging tone, which whisper only
    turns into junk text). Returns (text, turns)."""
    source, tmp = str(path), None
    if skip_s > 0:
        tmp, remaining = tones.trim_after(path, skip_s)
        if tmp is not None:
            if remaining < 0.4:  # nothing but the tone
                os.unlink(tmp)
                return "", []
            source = tmp
        else:
            skip_s = 0.0  # ffmpeg failed: transcribe the whole call as before
    try:
        segments, _info = model.transcribe(
            source, language=LANGUAGE, vad_filter=True, beam_size=5,
            condition_on_previous_text=False, initial_prompt=prompt or None,
            word_timestamps=bool(src_times))
        segments = list(segments)
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)
    text = " ".join(s.text.strip() for s in segments).strip()
    turns = []
    if src_times:
        # word times are relative to the trimmed audio; the radio start times are relative to the call
        words = [(w.start + skip_s, w.word) for s in segments for w in (s.words or [])]
        turns = split_turns(words, src_times)
    return text, turns


def main():
    init_db()
    model = load_model()
    prompt = build_prompt()
    while True:
        with connect() as con:
            row = con.execute(
                "SELECT id, audio_path, length_ms, src_times, tones_checked FROM calls "
                "WHERE transcribed_at IS NULL AND audio_deleted=0 AND attempts<? "
                "ORDER BY id DESC LIMIT 1", (MAX_ATTEMPTS,)).fetchone()
            if row is None:  # idle: catch up on tone scans for calls recorded before this feature existed
                old = con.execute(
                    "SELECT id, audio_path, length_ms FROM calls WHERE tones_checked=0 AND audio_deleted=0 "
                    "ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            try:
                if old is not None:
                    scan_tones(old["id"], old["audio_path"], old["length_ms"])
                else:
                    time.sleep(2)
            except Exception as exc:  # e.g. database busy: try again shortly
                print(f"tone backfill: {exc}", flush=True)
                time.sleep(5)
            continue
        started = time.time()
        try:
            turns = []
            if (row["length_ms"] or 0) > MAX_CALL_SECONDS * 1000:
                text = "[call too long to transcribe]"
                if row["tones_checked"] == 0:
                    scan_tones(row["id"], row["audio_path"], row["length_ms"])
            else:
                try:
                    times = json.loads(row["src_times"] or "[]")
                except ValueError:
                    times = []
                if row["tones_checked"] == 0:
                    scan_tones(row["id"], row["audio_path"], row["length_ms"])
                tone_end = stored_tone_end(row["id"])
                text, turns = transcribe(model, CAPTURE_DIR / row["audio_path"], times, prompt,
                                         skip_s=tone_end + 0.1 if tone_end > 0 else 0.0)
            with connect() as con:
                con.execute("UPDATE calls SET transcript=?, turns=?, transcribed_at=? WHERE id=?",
                            (text, json.dumps(turns) if turns else None, int(time.time()), row["id"]))
            print(f"call {row['id']}: {time.time() - started:.1f}s: {text[:80]!r}", flush=True)
        except Exception as exc:  # bad/partial file: retry a few times, then give up
            with connect() as con:
                con.execute("UPDATE calls SET attempts=attempts+1 WHERE id=?", (row["id"],))
            print(f"call {row['id']}: failed: {exc}", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
