"""ScanScribe transcription worker: faster-whisper on CPU. Transcribes new calls, newest first."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import CAPTURE_DIR, connect, init_db  # noqa: E402

MODEL = os.environ.get("WHISPER_MODEL", "small.en")
THREADS = int(os.environ.get("WHISPER_THREADS", "2"))
LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "en")
PROMPT = os.environ.get("WHISPER_PROMPT", "Police, fire and EMS radio dispatch in Concord, New Hampshire.")
MAX_ATTEMPTS = 3
# Calls longer than this are never sent to whisper (a stuck-open squelch can make multi-hour "calls")
MAX_CALL_SECONDS = int(os.environ.get("WHISPER_MAX_CALL_SECONDS", "300"))


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


def transcribe(model, path, src_times):
    segments, _info = model.transcribe(
        str(path), language=LANGUAGE, vad_filter=True, beam_size=5,
        condition_on_previous_text=False, initial_prompt=PROMPT or None,
        word_timestamps=bool(src_times))
    segments = list(segments)
    text = " ".join(s.text.strip() for s in segments).strip()
    turns = []
    if src_times:
        words = [(w.start, w.word) for s in segments for w in (s.words or [])]
        turns = split_turns(words, src_times)
    return text, turns


def main():
    init_db()
    model = load_model()
    while True:
        with connect() as con:
            row = con.execute(
                "SELECT id, audio_path, length_ms, src_times FROM calls WHERE transcribed_at IS NULL AND audio_deleted=0 AND attempts<? "
                "ORDER BY id DESC LIMIT 1", (MAX_ATTEMPTS,)).fetchone()
        if row is None:
            time.sleep(2)
            continue
        started = time.time()
        try:
            turns = []
            if (row["length_ms"] or 0) > MAX_CALL_SECONDS * 1000:
                text = "[call too long to transcribe]"
            else:
                try:
                    times = json.loads(row["src_times"] or "[]")
                except ValueError:
                    times = []
                text, turns = transcribe(model, CAPTURE_DIR / row["audio_path"], times)
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
