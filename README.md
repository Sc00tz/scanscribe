# ScanScribe

Self-hosted scanner recorder for Proxmox: an RTL-SDR dongle in an LXC container records
police/fire/EMS channels with [Trunk Recorder](https://github.com/TrunkRecorder/trunk-recorder).
Web UI (live feed + archive + player) is in; speech-to-text is next.

**Status:** recording and transcription verified on real hardware for the police channels. Web UI has live feed, archive, transcript search and a settings page (channels, gain/squelch, whisper model). No login on the web UI: LAN only.

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

## Paging tones

Fire services are alerted with a two-tone page (a long A tone, then a B tone). ScanScribe scans every recorded call for
these (in the transcription worker, using [icad_tone_detection](https://github.com/TheGreatCodeholio/icad_tone_detection), pinned to 2.9.0),
stores each tone pair with the call, and:

- shows a badge on the call: the name if you have named that pair, otherwise "Unknown tone 1218/1433";
- lets you name a pair once (click the badge, or use the **Tones** tab). Names are matched when the page is loaded,
  so naming a tone also labels every older call that has it. Pairs match when both tones are within 3 Hz;
- keeps the tone out of the transcript (whisper is given only the audio after the tone);
- skips the tone on playback by default. The **Skip paging tones** button in the player bar turns that off, and
  **Play tone** on the Tones tab plays just the tone from a recent call.

Only two-tone pages are detected (not long tones, warble, MDC or DTMF). The detector's own defaults expect a 2.6 s B tone,
which missed a real Capital Area page with a B tone of about 2 s, so `TONE_B_MIN` defaults to 1.5 s. Tune `TONE_A_MIN`,
`TONE_B_MIN` or switch it off with `TONE_DETECTION=off` in `/etc/scanscribe/scanscribe.env`, then restart
`scanscribe-transcribe`. Calls recorded before the feature existed are scanned in the background when the transcriber is idle.
The original audio is never modified.

## Layout

| Path | Purpose |
|------|---------|
| `install/scanscribe.sh` | Proxmox host installer |
| `container/setup.sh` | runs inside the LXC: deps, builds Trunk Recorder, installs service |
| `container/generate_config.py` | master channel CSV + env -> Trunk Recorder config |
| `web/` | web interface (stdlib Python, port 8080): live feed, archive, player, tone library |
| `web/tones.py` | paging-tone matching and grouping (stdlib); detection runs in the transcription worker |
| `config/` | default channel list and settings |
