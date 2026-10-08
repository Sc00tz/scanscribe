#!/usr/bin/env bash
# ScanScribe - Proxmox LXC installer. Run as root on the Proxmox VE host:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Sc00tz/scanscribe/main/install/scanscribe.sh)"
#
# Nothing is assumed: every setting is asked for (detected values are offered as
# choices), and every change to the Proxmox HOST is listed and needs a yes first.
set -Eeuo pipefail

REPO_URL="${SCANSCRIBE_REPO:-https://github.com/Sc00tz/scanscribe.git}"
REPO_BRANCH="${SCANSCRIBE_BRANCH:-main}"
# Realtek RTL-SDR USB IDs (2838 = RTL2838 DVB-T, 2832 = RTL2832U)
RTL_IDS=("0bda:2838" "0bda:2832")

die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }

[[ $EUID -eq 0 ]] || die "Run as root on the Proxmox host."
if ! command -v pct >/dev/null || ! command -v pveam >/dev/null; then die "pct/pveam not found - this must run on a Proxmox VE host."; fi

# ask VAR "Prompt" [default]   (empty answer uses default; no default = required)
ask() {
  local __var=$1 prompt=$2 def="${3:-}" ans
  while true; do
    if [[ -n $def ]]; then read -r -p "$prompt [$def]: " ans; ans="${ans:-$def}"
    else read -r -p "$prompt: " ans; fi
    [[ -n $ans ]] && break
    echo "A value is required."
  done
  printf -v "$__var" '%s' "$ans"
}
confirm() { local a; read -r -p "$1 [y/N]: " a; [[ $a =~ ^[Yy]$ ]]; }

# choose VAR "Prompt" item1 item2 ...   (numbered menu, no default)
choose() {
  local __var=$1 prompt=$2; shift 2
  local items=("$@") i n
  [[ ${#items[@]} -gt 0 ]] || die "Nothing to choose from for: $prompt"
  echo "$prompt"
  for i in "${!items[@]}"; do printf '  %d) %s\n' "$((i+1))" "${items[$i]}"; done
  while true; do
    read -r -p "Choice: " n
    if [[ $n =~ ^[0-9]+$ ]] && (( n >= 1 && n <= ${#items[@]} )); then
      printf -v "$__var" '%s' "${items[$((n-1))]}"; return
    fi
    echo "Enter a number from 1 to ${#items[@]}."
  done
}

# ---------------------------------------------------------------- dongle check
info "Looking for an RTL-SDR on this host"
found=()
for id in "${RTL_IDS[@]}"; do
  while IFS= read -r line; do found+=("$line"); done < <(lsusb -d "$id" 2>/dev/null || true)
done
if [[ ${#found[@]} -eq 0 ]]; then
  echo "No RTL-SDR (0bda:2838 / 0bda:2832) found by lsusb."
  confirm "Continue anyway (you can plug it in later)?" || exit 1
else
  printf 'Found:\n'; printf '  %s\n' "${found[@]}"
  if [[ ${#found[@]} -gt 1 ]]; then
    echo
    echo "NOTE: ${#found[@]} dongles are visible. ScanScribe stage 1 uses ONE (rtl=0, whichever the"
    echo "container enumerates first). To be certain which one is used, unplug the other for now."
  fi
fi

# Warn if a Proxmox VM already has an RTL-SDR passed through (it would hold the dongle)
vm_hits=$(grep -H -E '^usb[0-9]+:.*0bda:(2838|2832)' /etc/pve/qemu-server/*.conf 2>/dev/null || true)
if [[ -n $vm_hits ]]; then
  echo
  echo "WARNING: a VM has an RTL-SDR passed through. A running VM keeps the dongle, so the container cannot use it:"
  echo "$vm_hits"
  echo "Remove that usbN line from the VM (qm set <vmid> --delete usbN) if the VM doesn't need it."
  confirm "Continue anyway?" || exit 1
fi

# ------------------------------------------------------------------- settings
info "Container settings"
next_id=$(pvesh get /cluster/nextid)
ask CTID "Container ID" "$next_id"
pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID already exists."
ask HOSTNAME_CT "Hostname" "scanscribe"

mapfile -t rootfs_stores < <(pvesm status --content rootdir | awk 'NR>1 {print $1}')
choose STORAGE "Storage for the container disk:" "${rootfs_stores[@]}"
mapfile -t tmpl_stores < <(pvesm status --content vztmpl | awk 'NR>1 {print $1}')
choose TMPL_STORAGE "Storage for the OS template:" "${tmpl_stores[@]}"

ask DISK_GB "Disk size in GB (recordings + OS; stage 1 audio is small)" "32"
ask CORES "CPU cores" "2"
ask RAM_MB "RAM in MB (recorder + web + whisper transcription; 4096 proved too tight, 8192 is comfortable)" "8192"

mapfile -t bridges < <(ip -o link show type bridge | awk -F': ' '{print $2}')
choose BRIDGE "Network bridge:" "${bridges[@]}"
ask VLAN "VLAN tag (leave 0 for none)" "0"
choose NET_MODE "IP configuration:" "DHCP" "Static"
NET_ARGS="name=eth0,bridge=${BRIDGE},ip=dhcp"
if [[ $NET_MODE == Static ]]; then
  ask CT_IP "Container IP with prefix (e.g. 192.168.1.50/24)"
  ask CT_GW "Gateway"
  NET_ARGS="name=eth0,bridge=${BRIDGE},ip=${CT_IP},gw=${CT_GW}"
fi
[[ $VLAN != 0 ]] && NET_ARGS+=",tag=${VLAN}"
ask DNS "DNS server for the container (leave as 'host' to use the host's setting)" "host"
echo "Root login: you will be asked for a root password for the container."
read -r -s -p "Container root password: " CT_PASS; echo
[[ -n $CT_PASS ]] || die "Password required."

# --------------------------------------------------------------- host changes
cat <<MSG

------------------------------------------------------------------------
CHANGES THIS SCRIPT WILL MAKE TO THE PROXMOX HOST
  1. /etc/udev/rules.d/99-scanscribe-rtlsdr.rules   (new file)
       makes RTL-SDR USB devices 0bda:2838 and 0bda:2832 readable by the unprivileged container (mode 0666)
  2. /etc/modprobe.d/scanscribe-rtlsdr-blacklist.conf   (new file)
       stops the Linux DVB-T driver (dvb_usb_rtl28xxu and friends) from claiming the dongle,
       because that driver blocks SDR use. This affects EVERY RTL2832-based device on this host:
       if anything else on the host uses these dongles as a DVB-T TV tuner, it will stop working.
  3. Unload those modules now (modprobe -r) if they are currently loaded
  4. Append USB passthrough lines to /etc/pve/lxc/${CTID}.conf (the new container only)
No existing container or Proxmox setting is modified.
------------------------------------------------------------------------
MSG
if lsmod | grep -q '^dvb_usb_rtl28xxu'; then
  echo "dvb_usb_rtl28xxu is currently LOADED on this host."
  [[ -d /dev/dvb ]] && { echo "Existing DVB adapters:"; ls /dev/dvb; }
fi
confirm "Apply these host changes and create the container?" || die "Cancelled - nothing was changed."

# ----------------------------------------------------------------- host setup
info "Applying host changes"
cat > /etc/udev/rules.d/99-scanscribe-rtlsdr.rules <<'RULE'
# ScanScribe: let the LXC container use RTL-SDR dongles
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE:="0666"
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", MODE:="0666"
RULE
cat > /etc/modprobe.d/scanscribe-rtlsdr-blacklist.conf <<'BL'
# ScanScribe: keep the DVB-T driver off RTL-SDR dongles
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2832_sdr
blacklist rtl2830
blacklist r820t
BL
for m in rtl2832_sdr dvb_usb_rtl28xxu rtl2832 rtl2830 r820t; do
  lsmod | grep -q "^${m} " && { modprobe -r "$m" && echo "unloaded $m" || echo "could not unload $m (in use) - reboot or unplug/replug the dongle"; }
done
udevadm control --reload-rules && udevadm trigger --subsystem-match=usb

# ------------------------------------------------------------ template + CT
info "Fetching Ubuntu 24.04 template"
pveam update >/dev/null
TEMPLATE=$(pveam available --section system | awk '/ubuntu-24.04-standard/ {print $2}' | sort -V | tail -1)
[[ -n $TEMPLATE ]] || die "No Ubuntu 24.04 template found via pveam."
pveam list "$TMPL_STORAGE" | grep -q "$TEMPLATE" || pveam download "$TMPL_STORAGE" "$TEMPLATE"

info "Creating container $CTID"
create_args=(
  "$CTID" "${TMPL_STORAGE}:vztmpl/${TEMPLATE}"
  --hostname "$HOSTNAME_CT" --unprivileged 1 --onboot 1
  --cores "$CORES" --memory "$RAM_MB" --swap 512
  --rootfs "${STORAGE}:${DISK_GB}"
  --net0 "$NET_ARGS"
  --password "$CT_PASS"
  --features nesting=0
)
[[ $DNS != host ]] && create_args+=(--nameserver "$DNS")
pct create "${create_args[@]}"

cat >> "/etc/pve/lxc/${CTID}.conf" <<CONF
# ScanScribe: RTL-SDR USB passthrough
lxc.cgroup2.devices.allow: c 189:* rwm
lxc.mount.entry: /dev/bus/usb dev/bus/usb none bind,optional,create=dir
CONF

info "Starting container"
pct start "$CTID"
for _ in $(seq 1 30); do pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 && break; sleep 2; done
pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 || die "Container has no working network/DNS. Fix networking, then run inside it: git clone ${REPO_URL} /opt/scanscribe && /opt/scanscribe/container/setup.sh"

info "Installing ScanScribe inside the container (the Trunk Recorder build takes a while)"
pct exec "$CTID" -- bash -c "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates"
pct exec "$CTID" -- bash -c "rm -rf /opt/scanscribe && git clone --depth 1 --branch '${REPO_BRANCH}' '${REPO_URL}' /opt/scanscribe"
pct exec "$CTID" -- bash /opt/scanscribe/container/setup.sh

ct_addr=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')
cat <<DONE

ScanScribe stage 1 is installed in container ${CTID} (${ct_addr:-IP unknown}).
Check the dongle and recordings:   pct exec ${CTID} -- /usr/local/bin/scanscribe-check
Channel list to edit:              /etc/scanscribe/channels.master.csv (inside the container)
Settings (gain/squelch/etc.):      /etc/scanscribe/scanscribe.env
After editing:                     pct exec ${CTID} -- systemctl restart scanscribe-recorder
Recordings:                        /var/lib/scanscribe/recordings (inside the container)
DONE
