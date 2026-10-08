#!/usr/bin/env python3
"""Generate Trunk Recorder config.json + channel CSVs from the master channel list.

Master CSV columns:
  enabled, mode (analog|p25), freq_mhz, alpha_tag, description, category, tag,
  tone (CTCSS Hz, optional), nac (informational only), squelch (dB, optional)

Settings come from a simple KEY=VALUE env file (see config/scanscribe.env).
Run on every service start, so editing the master CSV + restarting applies changes.
"""
import argparse
import csv
import json
import os
import sys


class ConfigError(ValueError):
    """Invalid channel list or settings."""


DEFAULTS = {
    "SDR_DEVICE": "rtl=0",
    "SDR_RATE": "2400000",
    "SDR_GAIN": "40",
    "SDR_PPM": "0",
    "ANALOG_SQUELCH": "-50",
    "P25_SQUELCH": "-50",
    "P25_MODULATION": "fsk4",
    "CAPTURE_DIR": "/var/lib/scanscribe/recordings",
    "MIN_DURATION": "1.0",
    "MAX_DURATION": "300",
    "LOG_LEVEL": "info",
}


def load_env(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
    return cfg


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def load_channels(path):
    chans = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if not row.get("freq_mhz", "").strip():
                continue
            if not truthy(row.get("enabled", "true")):
                continue
            mode = row["mode"].strip().lower()
            if mode not in ("analog", "p25"):
                raise ConfigError(f"Bad mode {mode!r} for {row.get('alpha_tag')}; use analog or p25")
            hz = int(round(float(row["freq_mhz"]) * 1_000_000))
            row["_hz"] = hz
            row["_mode"] = mode
            chans.append(row)
    if not chans:
        raise ConfigError("No enabled channels in master list")
    return chans


def pick_center(freqs, rate, guard=30_000):
    """Centre the tuner on the channel span, nudged so no channel sits within
    `guard` Hz of the centre (RTL-SDRs have a DC spike there)."""
    lo, hi = min(freqs), max(freqs)
    center = (lo + hi) // 2
    # Round to the nearest 5 kHz for tidiness.
    center = int(round(center / 5000.0) * 5000)
    best = center
    for delta in range(0, 400_000, 5000):
        for cand in (center + delta, center - delta):
            if all(abs(f - cand) > guard for f in freqs):
                best = cand
                break
        else:
            continue
        break
    usable = int(rate * 0.4)  # +/- 40% of the sample rate is clean passband
    worst = max(abs(f - best) for f in freqs)
    if worst > usable:
        raise ConfigError(
            f"Channels span too wide for one dongle at {rate} S/s: farthest channel is "
            f"{worst/1e6:.3f} MHz from centre, usable is +/-{usable/1e6:.3f} MHz. "
            "Disable some channels or raise SDR_RATE."
        )
    return best, worst


def write_channel_file(path, chans, used_ids):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["TG Number", "Frequency", "Tone", "Alpha Tag", "Description",
                    "Tag", "Category", "Enable", "Signal Detector", "Squelch"])
        for c in chans:
            tg = int(round(c["_hz"] / 1000.0))  # stable ID: frequency in kHz
            while tg in used_ids:
                tg += 1
            used_ids.add(tg)
            c["_tg"] = tg
            w.writerow([tg, c["_hz"], (c.get("tone") or "").strip(),
                        c["alpha_tag"], c["description"], c["tag"], c["category"],
                        "", "", (c.get("squelch") or "").strip()])


def build(master, env, outdir):
    """Generate Trunk Recorder files in outdir; returns a one-line summary. Raises ConfigError."""
    return _build(load_env(env), load_channels(master), outdir)


def _build(cfg, chans, outdir):
    rate = int(cfg["SDR_RATE"])
    center, worst = pick_center([c["_hz"] for c in chans], rate)

    os.makedirs(outdir, exist_ok=True)
    used = set()
    analog = [c for c in chans if c["_mode"] == "analog"]
    p25 = [c for c in chans if c["_mode"] == "p25"]

    systems = []
    if analog:
        write_channel_file(os.path.join(outdir, "analog_channels.csv"), analog, used)
        systems.append({
            "type": "conventional", "shortName": "analog",
            "channelFile": "analog_channels.csv",
            "squelch": int(cfg["ANALOG_SQUELCH"]),
            "callLog": True, "audioArchive": True, "compressWav": True,
            "minDuration": float(cfg["MIN_DURATION"]),
            "maxDuration": float(cfg["MAX_DURATION"]),
        })
    if p25:
        write_channel_file(os.path.join(outdir, "p25_channels.csv"), p25, used)
        systems.append({
            "type": "conventionalP25", "shortName": "p25",
            "channelFile": "p25_channels.csv",
            "modulation": cfg["P25_MODULATION"],
            "squelch": int(cfg["P25_SQUELCH"]),
            "callLog": True, "audioArchive": True, "compressWav": True,
            "minDuration": float(cfg["MIN_DURATION"]),
            "maxDuration": float(cfg["MAX_DURATION"]),
        })

    source = {
        "center": center, "rate": rate, "gain": float(cfg["SDR_GAIN"]),
        "error": 0, "driver": "osmosdr", "device": cfg["SDR_DEVICE"],
    }
    if float(cfg["SDR_PPM"]):
        source["ppm"] = float(cfg["SDR_PPM"])

    config = {
        "ver": 2, "captureDir": cfg["CAPTURE_DIR"], "logLevel": cfg["LOG_LEVEL"],
        "callTimeout": 3, "sources": [source], "systems": systems,
    }
    with open(os.path.join(outdir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")

    return (f"{len(chans)} channels ({len(analog)} analog, {len(p25)} P25); "
          f"centre {center/1e6:.4f} MHz, rate {rate/1e6:.2f} MS/s, "
          f"farthest channel {worst/1e6:.3f} MHz from centre")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", default="/etc/scanscribe/channels.master.csv")
    ap.add_argument("--env", default="/etc/scanscribe/scanscribe.env")
    ap.add_argument("--outdir", default="/var/lib/scanscribe/tr")
    args = ap.parse_args()
    try:
        print(build(args.master, args.env, args.outdir))
    except ConfigError as exc:
        sys.exit(f"Config error: {exc}")


if __name__ == "__main__":
    main()
