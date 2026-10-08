"""ScanScribe transcription worker: faster-whisper on CPU. Transcribes new calls, newest first."""
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


def transcribe(model, path):
    segments, _info = model.transcribe(
        str(path), language=LANGUAGE, vad_filter=True, beam_size=5,
        condition_on_previous_text=False, initial_prompt=PROMPT or None)
    return " ".join(s.text.strip() for s in segments).strip()


def main():
    init_db()
    model = load_model()
    while True:
        with connect() as con:
            row = con.execute(
                "SELECT id, audio_path, length_ms FROM calls WHERE transcribed_at IS NULL AND attempts<? "
                "ORDER BY id DESC LIMIT 1", (MAX_ATTEMPTS,)).fetchone()
        if row is None:
            time.sleep(2)
            continue
        started = time.time()
        try:
            if (row["length_ms"] or 0) > MAX_CALL_SECONDS * 1000:
                text = "[call too long to transcribe]"
            else:
                text = transcribe(model, CAPTURE_DIR / row["audio_path"])
            with connect() as con:
                con.execute("UPDATE calls SET transcript=?, transcribed_at=? WHERE id=?",
                            (text, int(time.time()), row["id"]))
            print(f"call {row['id']}: {time.time() - started:.1f}s: {text[:80]!r}", flush=True)
        except Exception as exc:  # bad/partial file: retry a few times, then give up
            with connect() as con:
                con.execute("UPDATE calls SET attempts=attempts+1 WHERE id=?", (row["id"],))
            print(f"call {row['id']}: failed: {exc}", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
