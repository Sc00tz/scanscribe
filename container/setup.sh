#!/usr/bin/env bash
# Runs INSIDE the LXC (Ubuntu 24.04). Installs Trunk Recorder and ScanScribe stage 1.
set -Eeuo pipefail

TR_VERSION="${TR_VERSION:-v5.2.1}"
APP_DIR="/opt/scanscribe"
CONF_DIR="/etc/scanscribe"
DATA_DIR="/var/lib/scanscribe"

log() { printf '\n==> %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID}" == "ubuntu" && "${VERSION_ID}" == "24.04" ]] || {
  echo "This script targets Ubuntu 24.04 (found ${PRETTY_NAME:-unknown})." >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive

log "Installing packages"
apt-get update
apt-get install -y --no-install-recommends \
  build-essential ca-certificates cmake curl ffmpeg fdkaac git gnuradio gnuradio-dev \
  gr-osmosdr libboost-all-dev libcurl4-openssl-dev libgmp-dev liborc-0.4-dev \
  libpthread-stubs0-dev libsndfile1-dev libssl-dev libuhd-dev libusb-1.0-0-dev \
  pkg-config python3 rtl-sdr sox usbutils

log "Creating scanscribe user and directories"
id scanscribe >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin scanscribe
getent group plugdev >/dev/null || groupadd plugdev
usermod -aG plugdev scanscribe
install -d -o scanscribe -g scanscribe "$DATA_DIR" "$DATA_DIR/tr" "$DATA_DIR/recordings"
install -d "$CONF_DIR"

if [[ ! -x /usr/local/bin/trunk-recorder ]]; then
  log "Building Trunk Recorder ${TR_VERSION} (this takes a while)"
  build_root="$(mktemp -d /var/tmp/tr-build.XXXXXX)"
  git clone --depth 1 --branch "$TR_VERSION" https://github.com/TrunkRecorder/trunk-recorder.git "$build_root/src"
  mkdir "$build_root/build"
  cd "$build_root/build"
  # Each compile job needs roughly 1.5 GB; don't exceed available memory
  mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
  jobs=$(( mem_gb * 2 / 3 )); (( jobs < 1 )) && jobs=1
  cores=$(nproc); (( jobs > cores )) && jobs=$cores
  cmake "$build_root/src"
  make -j"$jobs"
  make install
  cd /
  rm -rf "$build_root"
else
  log "Trunk Recorder already installed, skipping build"
fi

log "Installing config (existing files in $CONF_DIR are kept)"
[[ -f "$CONF_DIR/channels.master.csv" ]] || install -m 0644 "$APP_DIR/config/channels.master.csv" "$CONF_DIR/channels.master.csv"
[[ -f "$CONF_DIR/scanscribe.env" ]]      || install -m 0644 "$APP_DIR/config/scanscribe.env" "$CONF_DIR/scanscribe.env"
install -m 0644 "$APP_DIR/systemd/scanscribe-recorder.service" /etc/systemd/system/scanscribe-recorder.service
install -m 0755 "$APP_DIR/container/scanscribe-check" /usr/local/bin/scanscribe-check

log "Generating Trunk Recorder config"
python3 "$APP_DIR/container/generate_config.py"
chown -R scanscribe:scanscribe "$DATA_DIR"

systemctl daemon-reload
if lsusb -d 0bda: >/dev/null 2>&1; then
  systemctl enable --now scanscribe-recorder.service
  echo "Recorder started."
else
  systemctl enable scanscribe-recorder.service
  echo "No Realtek RTL-SDR visible in the container; service enabled but not started."
  echo "Fix USB passthrough, then: systemctl start scanscribe-recorder"
fi

log "Done. Run 'scanscribe-check' for a dongle/recording status report."
