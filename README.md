# ScanScribe

Self-hosted scanner recorder for Proxmox: an RTL-SDR dongle in an LXC container records
police/fire/EMS channels with [Trunk Recorder](https://github.com/TrunkRecorder/trunk-recorder).
Planned next: speech-to-text (faster-whisper), a live feed, a searchable archive and a settings UI.

**Status: stage 1 (recording only).** Untested on real hardware as of this commit.

## Install (on the Proxmox host, as root)

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Sc00tz/scanscribe/main/install/scanscribe.sh)"
```

(Requires the repo to be public so the container can clone it.)

The script asks for every setting (CT ID, storage, network, size) and lists the host changes
before making them. Host changes: a udev rule for RTL-SDR dongles, a modprobe blacklist
for the Linux DVB-T driver, and USB passthrough lines in the new container's config.
**The blacklist affects every RTL2832 dongle on that host**, so don't use this on a host where a
dongle is used as a DVB-T TV tuner.

Install takes a while: Trunk Recorder is compiled from source (v5.2.1) inside the container.

## Stage 1 test checklist

1. Plug in only the dongle you want to use (the second one is ignored but makes `rtl=0` ambiguous).
2. `pct exec <ctid> -- scanscribe-check` shows the dongle, tuner test, service state and recordings.
3. Watch live: `pct exec <ctid> -- journalctl -fu scanscribe-recorder`.
4. Tune `/etc/scanscribe/scanscribe.env`:
   - `SDR_GAIN` (start 40), `ANALOG_SQUELCH` / `P25_SQUELCH` (start -50). Restart after changes.
5. Edit channels in `/etc/scanscribe/channels.master.csv`, then restart.

## Things to verify on air

- **P25 modulation**: `P25_MODULATION=fsk4` (C4FM) is a guess; try `qpsk` if the P25 channels record nothing intelligible.
- **Encryption**: if the police channels are encrypted, audio will be silent/garbled and cannot be transcribed.
- **NAC** column in the master list is informational only; Trunk Recorder's conventional P25 does not filter on it here.
- **Tone** column is blank, so each channel records any carrier above squelch. PL tones can be added later.
- **Coverage**: one dongle at 2.4 MS/s centred automatically (the generator moves the centre off any channel).
  Channels more than 0.96 MHz from the centre are rejected with an error rather than silently missed.
- The antenna matters more than any setting; a quarter-wave at ~155 MHz is about 48 cm.

## Layout

| Path | Purpose |
|------|---------|
| `install/scanscribe.sh` | Proxmox host installer |
| `container/setup.sh` | runs inside the LXC: deps, builds Trunk Recorder, installs service |
| `container/generate_config.py` | master channel CSV + env -> Trunk Recorder config |
| `config/` | default channel list and settings |
