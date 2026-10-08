"""ScanScribe web: indexes Trunk Recorder call logs into SQLite and serves a live feed + archive.
Standard library only (no pip/apt web framework needed)."""
import json
import os
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse


CAPTURE_DIR = Path(os.environ.get("CAPTURE_DIR", "/var/lib/scanscribe/recordings"))
DB_PATH = os.environ.get("DB_PATH", "/var/lib/scanscribe/scanscribe.db")
STATIC_DIR = Path(__file__).parent / "static"
SCAN_INTERVAL = float(os.environ.get("SCAN_INTERVAL", "2"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  json_path TEXT UNIQUE NOT NULL,
  audio_path TEXT NOT NULL,
  system TEXT, freq INTEGER, talkgroup INTEGER,
  tag TEXT, description TEXT, category TEXT, grp TEXT,
  start_ms INTEGER, length_ms INTEGER, src INTEGER,
  signal REAL, noise REAL, encrypted INTEGER, audio_type TEXT,
  transcript TEXT
);
CREATE INDEX IF NOT EXISTS calls_start ON calls(start_ms);
CREATE INDEX IF NOT EXISTS calls_tg ON calls(talkgroup, start_ms);
"""


def connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)
        con.execute("PRAGMA journal_mode=WAL")


def audio_for(json_path: Path) -> Optional[Path]:
    for ext in (".m4a", ".wav"):
        p = json_path.with_suffix(ext)
        if p.exists():
            return p
    return None


def ingest_once(con) -> int:
    """Index any call JSON whose audio exists. Returns number added."""
    known = {r[0] for r in con.execute("SELECT json_path FROM calls")}
    added = 0
    now = time.time()
    for jp in CAPTURE_DIR.rglob("*.json"):
        rel = str(jp.relative_to(CAPTURE_DIR))
        if rel in known:
            continue
        try:
            if now - jp.stat().st_mtime < 1.5:
                continue  # still being written
            audio = audio_for(jp)
            if audio is None:
                continue
            d = json.loads(jp.read_text())
        except (OSError, ValueError):
            continue
        src_list = d.get("srcList") or []
        con.execute(
            """INSERT OR IGNORE INTO calls
            (json_path, audio_path, system, freq, talkgroup, tag, description, category, grp,
             start_ms, length_ms, src, signal, noise, encrypted, audio_type)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rel, str(audio.relative_to(CAPTURE_DIR)), d.get("short_name"), d.get("freq"),
             d.get("talkgroup"), d.get("talkgroup_tag"), d.get("talkgroup_description"),
             d.get("talkgroup_group"), d.get("talkgroup_group_tag"),
             d.get("start_time_ms") or int(d.get("start_time", 0)) * 1000,
             d.get("call_length_ms") or int(d.get("call_length", 0)) * 1000,
             src_list[0].get("src") if src_list else None,
             d.get("signal"), d.get("noise"), d.get("encrypted", 0), d.get("audio_type")),
        )
        added += 1
    con.commit()
    return added


def ingest_loop():
    while True:
        try:
            with connect() as con:
                ingest_once(con)
        except Exception as exc:  # keep the indexer alive
            print(f"ingest error: {exc}", flush=True)
        time.sleep(SCAN_INTERVAL)


PUBLIC = ("id", "system", "freq", "talkgroup", "tag", "description", "category", "grp",
          "start_ms", "length_ms", "src", "signal", "noise", "encrypted", "audio_type", "transcript")
MIME = {".m4a": "audio/mp4", ".wav": "audio/wav"}


def int_arg(q, name):
    try:
        return int(q[name][0])
    except (KeyError, ValueError, IndexError):
        return None


def query_calls(q):
    where, args = [], []
    for name, col, op in (("talkgroup", "talkgroup", "="), ("start_after_ms", "start_ms", ">="),
                          ("start_before_ms", "start_ms", "<"), ("after_id", "id", ">"),
                          ("before_id", "id", "<")):
        v = int_arg(q, name)
        if v is not None:
            where.append(f"{col}{op}?")
            args.append(v)
    limit = min(max(int_arg(q, "limit") or 50, 1), 200)
    sql = "SELECT * FROM calls" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY id DESC LIMIT ?"
    with connect() as con:
        return [{k: r[k] for k in PUBLIC} for r in con.execute(sql, args + [limit])]


def query_channels():
    with connect() as con:
        rows = con.execute(
            """SELECT talkgroup, MAX(tag) AS tag, MAX(category) AS category, MAX(freq) AS freq,
                      COUNT(*) AS calls, MAX(start_ms) AS last_ms
               FROM calls GROUP BY talkgroup ORDER BY tag""").fetchall()
        return [dict(r) for r in rows]


class Handler(BaseHTTPRequestHandler):
    server_version = "ScanScribe"

    def log_message(self, fmt, *args):  # quiet
        pass

    def send_json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def fail(self, code):
        self.send_error(code)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/":
                self.send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8", range_ok=False)
            elif url.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif url.path == "/api/calls":
                self.send_json(query_calls(q))
            elif url.path == "/api/channels":
                self.send_json(query_channels())
            elif url.path.startswith("/audio/"):
                self.send_audio(url.path[len("/audio/"):])
            else:
                self.fail(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_audio(self, id_text):
        if not id_text.isdigit():
            return self.fail(404)
        with connect() as con:
            r = con.execute("SELECT audio_path FROM calls WHERE id=?", (int(id_text),)).fetchone()
        if not r:
            return self.fail(404)
        path = (CAPTURE_DIR / r["audio_path"]).resolve()
        if CAPTURE_DIR.resolve() not in path.parents or not path.is_file():
            return self.fail(404)
        self.send_file(path, MIME.get(path.suffix, "application/octet-stream"), range_ok=True)

    def send_file(self, path, mime, range_ok):
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if range_ok and rng and rng.startswith("bytes=") and "," not in rng:
            a, _, b = rng[6:].partition("-")
            try:
                if a:
                    start = int(a)
                    end = min(int(b), size - 1) if b else size - 1
                else:
                    start = max(size - int(b), 0)
            except ValueError:
                return self.fail(416)
            if start > end or start >= size:
                return self.fail(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        if range_ok:
            self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def main():
    init_db()
    threading.Thread(target=ingest_loop, daemon=True).start()
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
