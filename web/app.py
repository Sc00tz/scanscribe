"""ScanScribe web: indexes Trunk Recorder call logs into SQLite and serves a live feed + archive.
Standard library only (no pip/apt web framework needed)."""
import csv
import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import tempfile
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
MASTER_PATH = Path(os.environ.get("MASTER_PATH", "/etc/scanscribe/channels.master.csv"))
ENV_PATH = Path(os.environ.get("ENV_PATH", "/etc/scanscribe/scanscribe.env"))
APPLY_FLAG = Path(os.environ.get("APPLY_FLAG", "/var/lib/scanscribe/apply.flag"))
GEN_PATH = Path(os.environ.get(
    "GEN_PATH", str(Path(__file__).resolve().parent.parent / "container" / "generate_config.py")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  json_path TEXT UNIQUE NOT NULL,
  audio_path TEXT NOT NULL,
  system TEXT, freq INTEGER, talkgroup INTEGER,
  tag TEXT, description TEXT, category TEXT, grp TEXT,
  start_ms INTEGER, length_ms INTEGER, src INTEGER,
  signal REAL, noise REAL, encrypted INTEGER, audio_type TEXT,
  transcript TEXT,
  transcribed_at INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  audio_deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS units (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
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
        cols = {r[1] for r in con.execute("PRAGMA table_info(calls)")}
        if "transcribed_at" not in cols:  # upgrade a stage-2 database
            con.execute("ALTER TABLE calls ADD COLUMN transcribed_at INTEGER")
        if "audio_deleted" not in cols:
            con.execute("ALTER TABLE calls ADD COLUMN audio_deleted INTEGER NOT NULL DEFAULT 0")
        if "attempts" not in cols:
            con.execute("ALTER TABLE calls ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        for col, ddl in (("emergency", "INTEGER NOT NULL DEFAULT 0"), ("units", "TEXT"), ("err_pct", "INTEGER"),
                          ("src_times", "TEXT"), ("turns", "TEXT")):
            if col not in cols:
                con.execute(f"ALTER TABLE calls ADD COLUMN {col} {ddl}")
        con.execute("PRAGMA journal_mode=WAL")


def audio_for(json_path: Path) -> Optional[Path]:
    for ext in (".m4a", ".wav"):
        p = json_path.with_suffix(ext)
        if p.exists():
            return p
    return None


def call_extras(d: dict):
    """(emergency, units JSON, err_pct) from a Trunk Recorder call JSON."""
    src_list = d.get("srcList") or []
    units = []
    for s in src_list:
        u = s.get("src")
        if isinstance(u, int) and u > 0 and u not in units:
            units.append(u)
    emergency = 1 if d.get("emergency") or any(s.get("emergency") for s in src_list) else 0
    total = bad = 0.0
    for seg in d.get("freqList") or []:
        try:
            ln = float(seg.get("len") or 0)
            total += ln
            if seg.get("error_count"):
                bad += ln
        except (TypeError, ValueError):
            pass
    err_pct = round(100 * bad / total) if total > 0 and d.get("audio_type") == "digital" else None
    times = []
    for s_ in src_list:
        u, pos = s_.get("src"), s_.get("pos")
        if isinstance(u, int) and u > 0 and isinstance(pos, (int, float)):
            times.append([u, round(float(pos), 2)])
    return emergency, json.dumps(units), err_pct, json.dumps(times)


def backfill_extras(con):
    """Fill the new detail columns for calls indexed before they existed."""
    rows = con.execute("SELECT id, json_path FROM calls WHERE units IS NULL OR src_times IS NULL").fetchall()
    for r in rows:
        try:
            d = json.loads((CAPTURE_DIR / r["json_path"]).read_text())
        except (OSError, ValueError):
            d = {}
        con.execute("UPDATE calls SET emergency=?, units=?, err_pct=?, src_times=? WHERE id=?", (*call_extras(d), r["id"]))
    con.commit()


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
             start_ms, length_ms, src, signal, noise, encrypted, audio_type,
             emergency, units, err_pct, src_times)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rel, str(audio.relative_to(CAPTURE_DIR)), d.get("short_name"), d.get("freq"),
             d.get("talkgroup"), d.get("talkgroup_tag"), d.get("talkgroup_description"),
             d.get("talkgroup_group"), d.get("talkgroup_group_tag"),
             d.get("start_time_ms") or int(d.get("start_time", 0)) * 1000,
             d.get("call_length_ms") or int(d.get("call_length", 0)) * 1000,
             src_list[0].get("src") if src_list else None,
             d.get("signal"), d.get("noise"), d.get("encrypted", 0), d.get("audio_type"),
             *call_extras(d)),
        )
        added += 1
    con.commit()
    return added


def ingest_loop():
    try:
        with connect() as con:
            backfill_extras(con)
    except Exception as exc:
        print(f"backfill error: {exc}", flush=True)
    while True:
        try:
            with connect() as con:
                ingest_once(con)
        except Exception as exc:  # keep the indexer alive
            print(f"ingest error: {exc}", flush=True)
        time.sleep(SCAN_INTERVAL)


PUBLIC = ("id", "system", "freq", "talkgroup", "tag", "description", "category", "grp",
          "start_ms", "length_ms", "src", "signal", "noise", "encrypted", "audio_type", "transcript", "audio_deleted",
          "emergency", "units", "err_pct", "turns")
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
    text = (q.get("q") or [""])[0].strip()
    if text:
        esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("transcript LIKE ? ESCAPE '\\'")
        args.append(f"%{esc}%")
    limit = min(max(int_arg(q, "limit") or 50, 1), 200)
    sql = "SELECT * FROM calls" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY id DESC LIMIT ?"
    with connect() as con:
        out = []
        for r in con.execute(sql, args + [limit]):
            row = {k: r[k] for k in PUBLIC}
            for k in ("units", "turns"):
                try:
                    row[k] = json.loads(row[k] or "[]")
                except ValueError:
                    row[k] = []
            out.append(row)
        return out


def get_units():
    with connect() as con:
        return {str(r["id"]): r["name"] for r in con.execute("SELECT id, name FROM units")}


def set_unit(uid, name):
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise SettingsError("Radio ID must be a positive number.")
    name = " ".join(str(name or "").split())
    if len(name) > 40:
        raise SettingsError("Names can be up to 40 characters.")
    with connect() as con:
        if name:
            con.execute("INSERT INTO units (id, name) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name", (uid, name))
        else:
            con.execute("DELETE FROM units WHERE id=?", (uid,))
        con.commit()


def query_channels():
    with connect() as con:
        rows = con.execute(
            """SELECT talkgroup, MAX(tag) AS tag, MAX(category) AS category, MAX(freq) AS freq,
                      COUNT(*) AS calls, MAX(start_ms) AS last_ms
               FROM calls GROUP BY talkgroup ORDER BY tag""").fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------------------------------- retention
RETENTION_INTERVAL = float(os.environ.get("RETENTION_INTERVAL", "600"))


def retention_settings():
    """Read retention days from the env file each time, so Settings changes need no restart."""
    env = env_values() if ENV_PATH.exists() else {}

    def days(key, default):
        try:
            return max(int(env.get(key, default)), 0)
        except ValueError:
            return default
    return days("RETENTION_AUDIO_DAYS", 7), days("RETENTION_TEXT_DAYS", 0)


def _unlink(path):
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _inside(path):
    root = CAPTURE_DIR.resolve()
    try:
        return root in path.resolve().parents
    except OSError:
        return False


def run_retention(now=None):
    """Delete audio (and optionally whole call entries) older than the configured days.
    Returns (audio_removed, entries_removed). 0 days = keep forever."""
    audio_days, text_days = retention_settings()
    now = now or time.time()
    audio_removed = entries_removed = 0
    with connect() as con:
        if audio_days > 0:
            cutoff_ms = int((now - audio_days * 86400) * 1000)
            rows = con.execute("SELECT id, audio_path, json_path FROM calls "
                               "WHERE audio_deleted=0 AND start_ms<?", (cutoff_ms,)).fetchall()
            for r in rows:
                audio = CAPTURE_DIR / r["audio_path"]
                if _inside(audio):
                    for ext in (".m4a", ".wav", ".json"):
                        _unlink(audio.with_suffix(ext))
                jp = CAPTURE_DIR / r["json_path"]
                if _inside(jp):
                    _unlink(jp)
                con.execute("UPDATE calls SET audio_deleted=1 WHERE id=?", (r["id"],))
                audio_removed += 1
            # leftovers never indexed (e.g. interrupted writes), once clearly past the cutoff
            old = now - (audio_days + 1) * 86400
            for f in CAPTURE_DIR.rglob("*"):
                try:
                    if f.is_file() and f.suffix in (".wav", ".m4a", ".json") and f.stat().st_mtime < old:
                        f.unlink()
                except OSError:
                    pass
        if text_days > 0:
            cutoff_ms = int((now - text_days * 86400) * 1000)
            rows = con.execute("SELECT id, audio_path, json_path FROM calls WHERE start_ms<?", (cutoff_ms,)).fetchall()
            for r in rows:  # remove files too, or the indexer would re-add the entry
                audio = CAPTURE_DIR / r["audio_path"]
                if _inside(audio):
                    for ext in (".m4a", ".wav", ".json"):
                        _unlink(audio.with_suffix(ext))
                jp = CAPTURE_DIR / r["json_path"]
                if _inside(jp):
                    _unlink(jp)
            entries_removed = con.execute("DELETE FROM calls WHERE start_ms<?", (cutoff_ms,)).rowcount
        con.commit()
    # tidy empty folders (deepest first)
    for d in sorted((x for x in CAPTURE_DIR.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    return audio_removed, entries_removed


def retention_loop():
    while True:
        try:
            a, e = run_retention()
            if a or e:
                print(f"retention: removed audio for {a} calls, deleted {e} entries", flush=True)
        except Exception as exc:
            print(f"retention error: {exc}", flush=True)
        time.sleep(RETENTION_INTERVAL)


def storage_stats():
    total = count = 0
    for f in CAPTURE_DIR.rglob("*"):
        try:
            if f.is_file() and f.suffix in (".wav", ".m4a"):
                total += f.stat().st_size
                count += 1
        except OSError:
            pass
    with connect() as con:
        oldest = con.execute("SELECT MIN(start_ms) FROM calls").fetchone()[0]
        calls = con.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    return {"audio_bytes": total, "audio_files": count, "calls": calls, "oldest_ms": oldest}


# ------------------------------------------------------------------ settings
MASTER_COLUMNS = ["enabled", "mode", "freq_mhz", "alpha_tag", "description", "category",
                  "tag", "tone", "nac", "squelch"]
MAX_CHANNELS = 100


class SettingsError(ValueError):
    pass


def load_gen():
    spec = importlib.util.spec_from_file_location("scanscribe_generate_config", GEN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def clean_text(v, name, required=False, maxlen=80):
    v = "" if v is None else str(v).strip()
    if any(ord(ch) < 32 for ch in v):
        raise SettingsError(f"{name}: control characters are not allowed")
    if required and not v:
        raise SettingsError(f"{name} is required")
    if len(v) > maxlen:
        raise SettingsError(f"{name} is too long (max {maxlen})")
    return v


def clean_number(v, name, lo, hi, integer=False, allow_blank=False):
    v = "" if v is None else str(v).strip()
    if v == "":
        if allow_blank:
            return ""
        raise SettingsError(f"{name} is required")
    try:
        n = float(v)
    except ValueError:
        raise SettingsError(f"{name} must be a number")
    if not (lo <= n <= hi):
        raise SettingsError(f"{name} must be between {lo} and {hi}")
    if integer:
        if n != int(n):
            raise SettingsError(f"{name} must be a whole number")
        return str(int(n))
    return v


def clean_channels(rows):
    if not isinstance(rows, list) or not rows:
        raise SettingsError("The channel list is empty")
    if len(rows) > MAX_CHANNELS:
        raise SettingsError(f"Too many channels (max {MAX_CHANNELS})")
    out = []
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            raise SettingsError(f"Row {i}: bad format")
        label = (str(r.get("alpha_tag") or "").strip() or f"row {i}")
        try:
            mode = str(r.get("mode", "")).strip().lower()
            if mode not in ("analog", "p25"):
                raise SettingsError("mode must be analog or p25")
            out.append({
                "enabled": "true" if r.get("enabled") in (True, "true", "True", "1", 1) else "false",
                "mode": mode,
                "freq_mhz": clean_number(r.get("freq_mhz"), "frequency (MHz)", 30, 1000),
                "alpha_tag": clean_text(r.get("alpha_tag"), "name", required=True, maxlen=32),
                "description": clean_text(r.get("description"), "description"),
                "category": clean_text(r.get("category"), "category"),
                "tag": clean_text(r.get("tag"), "tag"),
                "tone": clean_number(r.get("tone"), "PL tone", 60, 260, allow_blank=True),
                "nac": clean_text(r.get("nac"), "NAC", maxlen=8),
                "squelch": clean_number(r.get("squelch"), "squelch", -120, 0, integer=True, allow_blank=True),
            })
        except SettingsError as exc:
            raise SettingsError(f"{label}: {exc}")
    return out


def channels_to_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=MASTER_COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def atomic_write(path, text):
    """Write via temp file + rename; keep one .bak of the previous version."""
    path = Path(path)
    if path.exists():
        try:
            (path.parent / (path.name + ".bak")).write_bytes(path.read_bytes())
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def validate_generation(master_text, env_text):
    """Run the real config generator on candidate files in a temp dir. Returns summary."""
    gen = load_gen()
    with tempfile.TemporaryDirectory() as td:
        m, e = os.path.join(td, "m.csv"), os.path.join(td, "e.env")
        Path(m).write_text(master_text, encoding="utf-8")
        Path(e).write_text(env_text, encoding="utf-8")
        try:
            return gen.build(m, e, os.path.join(td, "out"))
        except gen.ConfigError as exc:
            raise SettingsError(str(exc))
        except (ValueError, KeyError) as exc:
            raise SettingsError(f"Invalid settings: {exc}")


def clean_choice(v, options, name):
    if v not in options:
        raise SettingsError(f"{name} must be one of: {', '.join(options)}")
    return v


ENV_FIELDS = {
    "SDR_GAIN": lambda v: clean_number(v, "gain", 0, 50),
    "ANALOG_SQUELCH": lambda v: clean_number(v, "analog squelch", -120, 0, integer=True),
    "P25_SQUELCH": lambda v: clean_number(v, "P25 squelch", -120, 0, integer=True),
    "P25_MODULATION": lambda v: clean_choice(v, ("fsk4", "qpsk"), "P25 modulation"),
    "MIN_DURATION": lambda v: clean_number(v, "minimum call length", 0, 30),
    "WHISPER_MODEL": lambda v: clean_choice(v, ("tiny.en", "base.en", "small.en", "medium.en"), "whisper model"),
    "WHISPER_THREADS": lambda v: clean_number(v, "whisper threads", 1, 16, integer=True),
    "RETENTION_AUDIO_DAYS": lambda v: clean_number(v, "audio retention (days)", 0, 3650, integer=True),
    "RETENTION_TEXT_DAYS": lambda v: clean_number(v, "transcript retention (days)", 0, 3650, integer=True),
}


ENV_DEFAULTS = {"RETENTION_AUDIO_DAYS": "7", "RETENTION_TEXT_DAYS": "0"}  # in effect when absent from the file


def read_env_text():
    return ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""


def env_values():
    vals = {}
    for line in read_env_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def merged_env(changes):
    text = read_env_text()
    lines = text.splitlines()
    for key, val in changes.items():
        pat = re.compile(rf"^{re.escape(key)}=")
        for idx, line in enumerate(lines):
            if pat.match(line):
                lines[idx] = f"{key}={val}"
                break
        else:
            lines.append(f"{key}={val}")
    return "\n".join(lines) + "\n"


def read_master_rows():
    if not MASTER_PATH.exists():
        return []
    with open(MASTER_PATH, newline="", encoding="utf-8") as fh:
        rows = []
        for r in csv.DictReader(fh):
            rows.append({k: (r.get(k) or "") for k in MASTER_COLUMNS})
        return rows


def live_channels():
    out = []
    for r in read_master_rows():
        if str(r.get("enabled", "")).strip().lower() != "true":
            continue
        try:
            hz = int(round(float(r["freq_mhz"]) * 1_000_000))
        except (KeyError, ValueError):
            continue
        out.append({"freq_hz": hz, "name": r.get("alpha_tag", ""), "description": r.get("description", ""),
                    "category": r.get("category", ""), "mode": r.get("mode", "")})
    return out


def service_state(name):
    try:
        out = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def get_settings():
    env = env_values()
    return {
        "channels": read_master_rows(),
        "env": {k: env.get(k, ENV_DEFAULTS.get(k, "")) for k in ENV_FIELDS},
        "retention": {"audio_days": retention_settings()[0], "text_days": retention_settings()[1]},
        "storage": storage_stats(),
        "status": {"recorder": service_state("scanscribe-recorder"),
                   "transcribe": service_state("scanscribe-transcribe")},
    }


def save_channels(rows):
    clean = clean_channels(rows)
    text = channels_to_csv(clean)
    summary = validate_generation(text, read_env_text())
    atomic_write(MASTER_PATH, text)
    return summary


def save_env(values):
    if not isinstance(values, dict):
        raise SettingsError("bad format")
    changes = {}
    for key, val in values.items():
        if key not in ENV_FIELDS:
            raise SettingsError(f"Unknown setting {key}")
        changes[key] = ENV_FIELDS[key](str(val).strip())
    text = merged_env(changes)
    master = MASTER_PATH.read_text(encoding="utf-8") if MASTER_PATH.exists() else ""
    summary = validate_generation(master, text)
    atomic_write(ENV_PATH, text)
    return summary


def request_apply(what):
    allowed = {"recorder", "transcribe"}
    if not isinstance(what, list) or not what or not set(what) <= allowed:
        raise SettingsError("Nothing valid to apply")
    APPLY_FLAG.write_text(" ".join(sorted(set(what))) + "\n")


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
            elif url.path == "/api/live-channels":
                self.send_json(live_channels())
            elif url.path == "/api/units":
                self.send_json(get_units())
            elif url.path == "/api/codes":
                self.send_file(Path(__file__).parent / "codes.json", "application/json", range_ok=False)
            elif url.path == "/api/settings":
                self.send_json(get_settings())
            elif url.path == "/api/channels":
                self.send_json(query_channels())
            elif url.path.startswith("/audio/"):
                self.send_audio(url.path[len("/audio/"):])
            else:
                self.fail(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        url = urlparse(self.path)
        # CSRF guard: JSON bodies only, and any Origin must match this server's Host
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self.fail(403)
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self.fail(415)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.fail(400)
        if n <= 0 or n > 262144:
            return self.fail(413)
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            return self.fail(400)
        try:
            if url.path == "/api/settings/channels":
                summary = save_channels(body.get("channels"))
                result = {"ok": True, "summary": summary}
            elif url.path == "/api/settings/env":
                summary = save_env(body.get("values"))
                result = {"ok": True, "summary": summary}
            elif url.path == "/api/units":
                set_unit(body.get("id"), body.get("name"))
                result = {"ok": True}
            elif url.path == "/api/apply":
                request_apply(body.get("what"))
                result = {"ok": True}
            else:
                return self.fail(404)
        except SettingsError as exc:
            result = {"ok": False, "error": str(exc)}
        except OSError as exc:
            result = {"ok": False, "error": f"Could not write: {exc.strerror or exc}"}
        self.send_json(result)

    def send_audio(self, id_text):
        if not id_text.isdigit():
            return self.fail(404)
        with connect() as con:
            r = con.execute("SELECT audio_path FROM calls WHERE id=? AND audio_deleted=0", (int(id_text),)).fetchone()
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
    threading.Thread(target=retention_loop, daemon=True).start()
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
