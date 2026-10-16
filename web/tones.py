"""ScanScribe paging tones: two-tone sequential pages (Quick Call style, a long A tone then a B tone).

The top level is standard library only, so the web server (which has no pip packages) can import this
for matching and grouping. The detector needs numpy/scipy/pydub and is imported lazily, so only the
transcription worker (which runs in the virtualenv) ever loads it.
"""
import os
import subprocess
import tempfile

# Two tone pairs count as the same when both frequencies are within this many Hz of each other.
# Measured detector accuracy on a real call was about 1 Hz; 3 Hz leaves room for radio drift.
TOL_HZ = 3.0

# Shortest A and B tone to accept (seconds). The library's own defaults (0.85 / 2.6) missed a real
# Capital Area page whose B tone is about 2 s long, so these are looser. Override in scanscribe.env.
# Only a tone that starts within this many seconds of the call's start is skipped in playback and left
# out of the transcript. Pages come first; a detection later in a call is left alone.
SKIP_WINDOW_S = 15.0

A_MIN = float(os.environ.get("TONE_A_MIN", "0.7"))
B_MIN = float(os.environ.get("TONE_B_MIN", "1.5"))


def detect(path):
    """Two-tone pages found in an audio file: [{freq_a, freq_b, a_len, b_len, start, end}] (seconds).
    Raises if the file cannot be read. Only two-tone detection is switched on; the library's other
    detectors (long tone, warble, pulsed, MDC, DTMF) are not used, which keeps it fast and quiet."""
    from icad_tone_detection import tone_detect  # lazy: needs numpy, scipy, pydub and ffmpeg
    result = tone_detect(
        str(path), tone_a_min_length=A_MIN, tone_b_min_length=B_MIN,
        detect_pulsed=False, detect_long=False, detect_hi_low=False, detect_mdc=False, detect_dtmf=False)
    events = []
    for t in result.two_tone_result or []:
        freqs = t.get("detected") or []
        if len(freqs) != 2:
            continue
        events.append({
            "freq_a": round(float(freqs[0]), 1), "freq_b": round(float(freqs[1]), 1),
            "a_len": round(float(t.get("tone_a_length") or 0), 2),
            "b_len": round(float(t.get("tone_b_length") or 0), 2),
            "start": round(float(t.get("start") or 0), 2), "end": round(float(t.get("end") or 0), 2)})
    return events


def match(freq_a, freq_b, library):
    """The library entry closest to this pair within its tolerance, or None.
    library: dicts with freq_a, freq_b and optional tol_hz."""
    best, best_d = None, None
    for entry in library:
        tol = float(entry.get("tol_hz") or TOL_HZ)
        d = max(abs(freq_a - entry["freq_a"]), abs(freq_b - entry["freq_b"]))
        if d <= tol and (best_d is None or d < best_d):
            best, best_d = entry, d
    return best


def cluster(events, tol=TOL_HZ):
    """Group tone events whose A and B frequencies agree within tol of the group's average.
    events: dicts with freq_a, freq_b and optionally call_id, start_ms, sample (any payload).
    Returns [{freq_a, freq_b, count, last_ms, sample}] where sample comes from the newest event."""
    groups = []
    for e in sorted(events, key=lambda x: (x["freq_a"], x["freq_b"])):
        for g in groups:
            if abs(e["freq_a"] - g["sum_a"] / g["count"]) <= tol and abs(e["freq_b"] - g["sum_b"] / g["count"]) <= tol:
                break
        else:
            g = {"sum_a": 0.0, "sum_b": 0.0, "count": 0, "last_ms": None, "sample": None}
            groups.append(g)
        g["sum_a"] += e["freq_a"]
        g["sum_b"] += e["freq_b"]
        g["count"] += 1
        ms = e.get("start_ms")
        if ms is not None and (g["last_ms"] is None or ms >= g["last_ms"]):
            g["last_ms"], g["sample"] = ms, e.get("sample")
    return [{"freq_a": round(g["sum_a"] / g["count"], 1), "freq_b": round(g["sum_b"] / g["count"], 1),
             "count": g["count"], "last_ms": g["last_ms"], "sample": g["sample"]} for g in groups]


def trim_after(path, start_s):
    """Copy of the audio from start_s onward as a 16 kHz mono WAV in a temp file, for the transcriber.
    Returns (temp_path, seconds_remaining), or (None, 0.0) if ffmpeg could not do it.
    The caller deletes the temp file."""
    fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="scanscribe-trim-")
    os.close(fd)
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start_s:.2f}", "-i", str(path),
                        "-ac", "1", "-ar", "16000", "-f", "wav", tmp],
                       check=True, capture_output=True, timeout=60)
        remaining = max(os.path.getsize(tmp) - 44, 0) / 32000.0  # 16-bit mono at 16 kHz
        return tmp, remaining
    except (OSError, subprocess.SubprocessError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None, 0.0
