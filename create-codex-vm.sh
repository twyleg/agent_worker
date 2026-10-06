#!/usr/bin/env bash
set -euo pipefail

# Zero-touch disposable Xubuntu VM for Codex on an Arch Linux host.
#
# This script:
#   * installs the required Arch/libvirt tooling
#   * downloads and verifies the Xubuntu 26.04 Minimal ISO if needed
#   * creates a dedicated SSH key for the VM
#   * generates Ubuntu autoinstall/cloud-init data
#   * creates and installs the VM completely unattended
#   * boots the installed system
#   * waits for cloud-init provisioning to finish
#
# Nothing from the host is shared with the guest by default. Use
# share-project.sh to expose one selected project afterwards.
#
# Usage:
#   ./create-codex-vm.sh
#   ./create-codex-vm.sh --recreate
#
# Optional environment overrides:
#   VM_NAME=codex-xubuntu
#   RAM_MB=12288
#   VCPUS=6
#   DISK_GB=64
#   XUBUNTU_SERIES=26.04
#   TIMEZONE=Europe/Berlin
#   LOCALE=en_US.UTF-8
#   KEYBOARD_LAYOUT=de

VM_NAME="${VM_NAME:-codex-xubuntu}"
RAM_MB="${RAM_MB:-12288}"
VCPUS="${VCPUS:-6}"
DISK_GB="${DISK_GB:-64}"
XUBUNTU_SERIES="${XUBUNTU_SERIES:-26.04}"
TIMEZONE="${TIMEZONE:-Europe/Berlin}"
LOCALE="${LOCALE:-en_US.UTF-8}"
KEYBOARD_LAYOUT="${KEYBOARD_LAYOUT:-de}"
KEYBOARD_VARIANT="${KEYBOARD_VARIANT:-}"
CODEX_USER="${CODEX_USER:-codex}"
GUEST_HOSTNAME="${GUEST_HOSTNAME:-codex-vm}"
LIBVIRT_URI="${LIBVIRT_URI:-qemu:///system}"
IMAGE_DIR="${IMAGE_DIR:-/var/lib/libvirt/images/codex-vm}"
ISO_DIR="$IMAGE_DIR/iso"
DISK_PATH="$IMAGE_DIR/${VM_NAME}.qcow2"
RELEASE_URL="https://cdimage.ubuntu.com/xubuntu/releases/${XUBUNTU_SERIES}/release"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/codex-vm"
SEED_DIR="$STATE_DIR/autoinstall"
SSH_KEY="$STATE_DIR/id_ed25519"
KNOWN_HOSTS="$STATE_DIR/known_hosts"
PASSWORD_FILE="$STATE_DIR/${VM_NAME}.password"
EXPORT_DIR="${CODEX_EXPORT_DIR:-/var/lib/libvirt/codex-share/current}"
SHARE_STATE_XML="$STATE_DIR/current-share.xml"
SHARE_STATE_PATH="$STATE_DIR/current-share.path"
READY_TIMEOUT="${READY_TIMEOUT:-900}"
RECREATE=0

if [[ "${1:-}" == "--recreate" ]]; then
  RECREATE=1
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--recreate]" >&2
  exit 2
fi

log() { printf '\n==> %s\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] || die "Run this script as your normal user, not as root. It uses sudo where needed."
[[ -e /dev/kvm ]] || die "/dev/kvm does not exist. Enable Intel VT-x/AMD-V and load the KVM modules."

if [[ "$(id -u):$(id -g)" != "1000:1000" ]]; then
  echo "WARNING: Host UID:GID is $(id -u):$(id -g), while the first Xubuntu user is normally 1000:1000." >&2
  echo "         virtiofs passthrough is simplest when these IDs match." >&2
fi

install_arch_packages() {
  command -v pacman >/dev/null 2>&1 || die "This script currently targets an Arch Linux host (pacman not found)."

  local packages=(
    qemu-desktop
    libvirt
    virt-install
    virt-viewer
    dnsmasq
    edk2-ovmf
    virtiofsd
    curl
    openssh
    openssl
  )

  log "Ensuring required Arch packages are installed"
  sudo pacman -S --needed --noconfirm "${packages[@]}"
}

ensure_libvirt() {
  log "Starting libvirt"
  sudo systemctl enable --now libvirtd.service

  if ! sudo virsh -c "$LIBVIRT_URI" net-info default >/dev/null 2>&1; then
    log "Creating libvirt default NAT network"
    if [[ -r /usr/share/libvirt/networks/default.xml ]]; then
      sudo virsh -c "$LIBVIRT_URI" net-define /usr/share/libvirt/networks/default.xml >/dev/null
    else
      local netxml
      netxml="$(mktemp)"
      cat >"$netxml" <<'XML'
<network>
  <name>default</name>
  <forward mode='nat'/>
  <bridge name='virbr0' stp='on' delay='0'/>
  <ip address='192.168.122.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='192.168.122.2' end='192.168.122.254'/>
    </dhcp>
  </ip>
</network>
XML
      sudo virsh -c "$LIBVIRT_URI" net-define "$netxml" >/dev/null
      rm -f "$netxml"
    fi
  fi

  sudo virsh -c "$LIBVIRT_URI" net-autostart default >/dev/null
  if ! sudo virsh -c "$LIBVIRT_URI" net-info default | grep -qE '^Active:[[:space:]]+yes$'; then
    sudo virsh -c "$LIBVIRT_URI" net-start default >/dev/null
  fi
}

find_latest_iso_name() {
  curl -fsSL "$RELEASE_URL/" \
    | grep -oE "xubuntu-${XUBUNTU_SERIES}(\.[0-9]+)?-minimal-amd64\.iso" \
    | sort -Vu \
    | tail -n1
}

download_iso() {
  sudo install -d -m 0755 "$ISO_DIR"

  log "Looking up the latest Xubuntu ${XUBUNTU_SERIES} Minimal ISO"
  ISO_NAME="$(find_latest_iso_name)"
  [[ -n "$ISO_NAME" ]] || die "Could not determine a Xubuntu Minimal ISO from $RELEASE_URL"
  ISO_PATH="$ISO_DIR/$ISO_NAME"

  if [[ ! -f "$ISO_PATH" ]]; then
    log "Downloading $ISO_NAME"
    sudo curl -fL --progress-bar -o "${ISO_PATH}.part" "$RELEASE_URL/$ISO_NAME"
    sudo mv "${ISO_PATH}.part" "$ISO_PATH"
    sudo chmod 0644 "$ISO_PATH"
  else
    log "ISO already present: $ISO_PATH"
  fi

  log "Verifying ISO SHA-256 checksum"
  local sums expected actual
  sums="$(mktemp)"
  curl -fsSL "$RELEASE_URL/SHA256SUMS" -o "$sums"
  expected="$(awk -v f="$ISO_NAME" '$2 == "*"f || $2 == f {print $1}' "$sums" | head -n1)"
  rm -f "$sums"
  [[ -n "$expected" ]] || die "Could not find checksum for $ISO_NAME"
  actual="$(sha256sum "$ISO_PATH" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || die "Checksum mismatch for $ISO_PATH"
  log "Checksum OK"
}

ensure_vm_ssh_key() {
  mkdir -p "$STATE_DIR"
  chmod 0700 "$STATE_DIR"

  if [[ ! -f "$SSH_KEY" ]]; then
    log "Creating dedicated SSH key for the disposable VM"
    ssh-keygen -q -t ed25519 -N '' -C 'codex-vm' -f "$SSH_KEY"
  fi
  chmod 0600 "$SSH_KEY"
}

generate_guest_password() {
  # This password is for local graphical/console login. SSH password login is
  # disabled. Store it only in the user's private state directory.
  if [[ ! -f "$PASSWORD_FILE" || $RECREATE -eq 1 ]]; then
    local pw
    pw="$(openssl rand -hex 12)"
    printf '%s\n' "$pw" >"$PASSWORD_FILE"
    chmod 0600 "$PASSWORD_FILE"
  fi
  GUEST_PASSWORD="$(cat "$PASSWORD_FILE")"
  PASSWORD_HASH="$(openssl passwd -6 "$GUEST_PASSWORD")"
}

generate_autoinstall() {
  local pubkey
  pubkey="$(cat "${SSH_KEY}.pub")"
  mkdir -p "$SEED_DIR"
  chmod 0700 "$SEED_DIR"

  # user-data is consumed by cloud-init in the installer. The nested user-data
  # section is then carried into the installed system and executed on first boot.
  cat >"$SEED_DIR/user-data" <<EOF2
#cloud-config
autoinstall:
  version: 1
  source:
    id: xubuntu-desktop-minimal
    search_drivers: false
  locale: ${LOCALE}
  keyboard:
    layout: ${KEYBOARD_LAYOUT}
    variant: "${KEYBOARD_VARIANT}"
  timezone: ${TIMEZONE}
  identity:
    hostname: ${GUEST_HOSTNAME}
    username: ${CODEX_USER}
    password: '${PASSWORD_HASH}'
  ssh:
    install-server: true
    allow-pw: false
    authorized-keys:
      - '${pubkey}'
  storage:
    layout:
      name: direct
  packages:
    - ca-certificates
    - curl
    - wget
    - gnupg
    - git
    - git-lfs
    - build-essential
    - pkg-config
    - cmake
    - ninja-build
    - jq
    - ripgrep
    - fd-find
    - file
    - tree
    - less
    - vim
    - unzip
    - zip
    - rsync
    - python3
    - python3-pip
    - python3-venv
    - pipx
    - nodejs
    - npm
    - openjdk-21-jdk
    - xvfb
    - xauth
    - x11-utils
    - dbus-x11
    - weston
    - mesa-utils
    - xdg-utils
    - qemu-guest-agent
    - spice-vdagent
  updates: security
  shutdown: poweroff
  user-data:
    ssh_pwauth: false
    disable_root: true
    write_files:
      - path: /etc/sudoers.d/90-${CODEX_USER}
        owner: root:root
        permissions: '0440'
        content: |
          ${CODEX_USER} ALL=(ALL:ALL) NOPASSWD: ALL
      - path: /usr/local/bin/codex-workspace-mount
        owner: root:root
        permissions: '0755'
        content: |
          #!/usr/bin/env bash
          set -euo pipefail
          MOUNTPOINT="/home/${CODEX_USER}/workspace"
          TAG="codex-project"
          mkdir -p "\$MOUNTPOINT"
          if findmnt -rn -T "\$MOUNTPOINT" -t virtiofs >/dev/null 2>&1; then
            echo "Already mounted: \$MOUNTPOINT"
            exit 0
          fi
          mount -t virtiofs "\$TAG" "\$MOUNTPOINT"
          echo "Mounted \$TAG at \$MOUNTPOINT"
      - path: /usr/local/bin/codex-workspace-unmount
        owner: root:root
        permissions: '0755'
        content: |
          #!/usr/bin/env bash
          set -euo pipefail
          MOUNTPOINT="/home/${CODEX_USER}/workspace"
          if findmnt -rn -T "\$MOUNTPOINT" -t virtiofs >/dev/null 2>&1; then
            umount "\$MOUNTPOINT"
            echo "Unmounted \$MOUNTPOINT"
          else
            echo "Not mounted: \$MOUNTPOINT"
          fi
    runcmd:
      - [mkdir, -p, /home/${CODEX_USER}/workspace]
      - [chown, '${CODEX_USER}:${CODEX_USER}', /home/${CODEX_USER}/workspace]
      - [bash, -lc, "command -v fdfind >/dev/null && ln -sf /usr/bin/fdfind /usr/local/bin/fd || true"]
      - [systemctl, enable, --now, qemu-guest-agent.service]
      - [npm, install, -g, '@openai/codex@latest']
      - [bash, -lc, "echo 'Codex VM provisioning complete' > /var/lib/codex-vm-ready"]
EOF2

  cat >"$SEED_DIR/meta-data" <<EOF2
instance-id: ${VM_NAME}-$(date +%s)
local-hostname: ${GUEST_HOSTNAME}
EOF2

  chmod 0600 "$SEED_DIR/user-data" "$SEED_DIR/meta-data"
}

cleanup_stale_share() {
  # If a previous disposable VM was destroyed while a project was attached,
  # make sure the host bind mount and helper state do not survive recreation.
  if findmnt -rn -M "$EXPORT_DIR" >/dev/null 2>&1; then
    log "Removing stale project bind mount"
    sudo umount "$EXPORT_DIR" || sudo umount -l "$EXPORT_DIR" || true
  fi
  rm -f "$SHARE_STATE_XML" "$SHARE_STATE_PATH"
}

remove_existing_vm() {
  if sudo virsh -c "$LIBVIRT_URI" dominfo "$VM_NAME" >/dev/null 2>&1; then
    if [[ $RECREATE -ne 1 ]]; then
      die "VM '$VM_NAME' already exists. Re-run with --recreate to destroy and replace it."
    fi

    log "Removing existing VM '$VM_NAME'"
    if sudo virsh -c "$LIBVIRT_URI" domstate "$VM_NAME" 2>/dev/null | grep -qviE 'shut off|shutoff'; then
      sudo virsh -c "$LIBVIRT_URI" destroy "$VM_NAME" >/dev/null || true
    fi
    sudo virsh -c "$LIBVIRT_URI" undefine "$VM_NAME" --nvram >/dev/null 2>&1 \
      || sudo virsh -c "$LIBVIRT_URI" undefine "$VM_NAME" >/dev/null
  fi

  if [[ $RECREATE -eq 1 && -f "$DISK_PATH" ]]; then
    sudo rm -f "$DISK_PATH"
  fi

  [[ $RECREATE -eq 1 ]] && cleanup_stale_share
}

create_and_install_vm() {
  sudo install -d -m 0755 "$IMAGE_DIR"
  sudo install -d -m 0755 /var/lib/libvirt/codex-share/current

  log "Creating '$VM_NAME' and starting unattended Xubuntu installation"
  log "Installer progress is available with: sudo virsh console $VM_NAME"

  # --location is used rather than --cdrom so virt-install can append the
  # mandatory 'autoinstall' kernel argument. The cloud-init option creates a
  # temporary NoCloud seed ISO containing our user-data/meta-data.
  sudo virt-install \
    --connect "$LIBVIRT_URI" \
    --name "$VM_NAME" \
    --memory "$RAM_MB" \
    --vcpus "$VCPUS" \
    --cpu host-passthrough \
    --machine q35 \
    --boot uefi \
    --disk "path=$DISK_PATH,size=$DISK_GB,format=qcow2,bus=virtio,discard=unmap" \
    --location "$ISO_PATH,kernel=casper/vmlinuz,initrd=casper/initrd" \
    --extra-args "autoinstall console=ttyS0,115200n8" \
    --cloud-init "user-data=$SEED_DIR/user-data,meta-data=$SEED_DIR/meta-data" \
    --network network=default,model=virtio \
    --graphics spice \
    --video virtio \
    --console pty,target.type=serial \
    --channel unix,target.type=virtio,target.name=org.qemu.guest_agent.0 \
    --memorybacking source.type=memfd,access.mode=shared \
    --osinfo detect=on,require=off \
    --noreboot \
    --noautoconsole \
    --wait=-1

  log "Installation completed; booting installed system"
  sudo virsh -c "$LIBVIRT_URI" start "$VM_NAME" >/dev/null
}

vm_mac() {
  sudo virsh -c "$LIBVIRT_URI" domiflist "$VM_NAME" \
    | awk '$3 == "network" || $2 == "network" {print $5; exit}'
}

find_vm_ip() {
  local ip mac
  ip="$(sudo virsh -c "$LIBVIRT_URI" domifaddr "$VM_NAME" --source agent 2>/dev/null \
        | awk '/ipv4/ {sub(/\/.*/, "", $4); print $4; exit}')"
  if [[ -n "$ip" ]]; then
    printf '%s\n' "$ip"
    return 0
  fi

  mac="$(vm_mac)"
  if [[ -n "$mac" ]]; then
    ip="$(sudo virsh -c "$LIBVIRT_URI" net-dhcp-leases default 2>/dev/null \
          | awk -v m="$mac" 'tolower($3)==tolower(m) {sub(/\/.*/, "", $5); print $5; exit}')"
    [[ -n "$ip" ]] && printf '%s\n' "$ip" && return 0
  fi
  return 1
}

wait_for_guest() {
  log "Waiting for the installed guest and first-boot provisioning"
  local deadline ip=""
  deadline=$((SECONDS + READY_TIMEOUT))

  while (( SECONDS < deadline )); do
    ip="$(find_vm_ip || true)"
    if [[ -n "$ip" ]]; then
      if ssh -i "$SSH_KEY" \
          -o BatchMode=yes \
          -o ConnectTimeout=3 \
          -o StrictHostKeyChecking=accept-new \
          -o "UserKnownHostsFile=$KNOWN_HOSTS" \
          "${CODEX_USER}@${ip}" true >/dev/null 2>&1; then
        break
      fi
    fi
    sleep 3
  done

  [[ -n "$ip" ]] || die "VM booted but no IP address was discovered. Check: sudo virsh console $VM_NAME"
  if (( SECONDS >= deadline )); then
    die "VM did not become reachable over SSH. Check: sudo virsh console $VM_NAME"
  fi

  log "VM reachable at $ip; waiting for cloud-init"
  if ! ssh -i "$SSH_KEY" \
      -o BatchMode=yes \
      -o StrictHostKeyChecking=accept-new \
      -o "UserKnownHostsFile=$KNOWN_HOSTS" \
      "${CODEX_USER}@${ip}" 'sudo cloud-init status --wait'; then
    echo "WARNING: cloud-init reported an error. Inspect with:" >&2
    echo "  ssh -i '$SSH_KEY' ${CODEX_USER}@${ip} 'sudo cloud-init status --long; sudo journalctl -u cloud-final'" >&2
    return 1
  fi

  VM_IP="$ip"
}

print_summary() {
  local codex_version
  codex_version="$(ssh -i "$SSH_KEY" \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new \
    -o "UserKnownHostsFile=$KNOWN_HOSTS" \
    "${CODEX_USER}@${VM_IP}" 'codex --version 2>/dev/null || echo not-installed')"

  cat <<EOF2

============================================================
Codex VM is ready
============================================================

VM:             $VM_NAME
IP:             $VM_IP
User:           $CODEX_USER
Codex:          $codex_version
GUI password:   $(cat "$PASSWORD_FILE")
SSH key:        $SSH_KEY

SSH:
  ssh -i "$SSH_KEY" \\
    -o UserKnownHostsFile="$KNOWN_HOSTS" \\
    ${CODEX_USER}@${VM_IP}

Graphical console:
  sudo virt-viewer --connect "$LIBVIRT_URI" "$VM_NAME"

Authenticate Codex once inside the VM:
  codex

Then expose one project from the host:
  ./share-project.sh attach ~/dev/workspace/my-project

Nothing from the host is shared with the VM until you explicitly attach a
project. The VM has no access to your host SSH keys or home directory.
EOF2
}

install_arch_packages
ensure_libvirt
download_iso
ensure_vm_ssh_key
generate_guest_password
generate_autoinstall
remove_existing_vm
create_and_install_vm
wait_for_guest
print_summary
