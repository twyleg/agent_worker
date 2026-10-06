#!/usr/bin/env bash
set -euo pipefail

# Optional repair/update helper for an already installed Codex VM.
# Fresh VMs created by create-codex-vm.sh are provisioned automatically and do
# not require this script.
#
# Run inside the VM:
#   sudo bash setup-codex-guest.sh

CODEX_USER="${CODEX_USER:-codex}"
WORKSPACE="/home/${CODEX_USER}/workspace"
VIRTIOFS_TAG="codex-project"

log() { printf '\n==> %s\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run this script with sudo/root."
id "$CODEX_USER" >/dev/null 2>&1 || die "User '$CODEX_USER' does not exist."

export DEBIAN_FRONTEND=noninteractive

log "Updating package metadata"
apt-get update

log "Installing/updating development and GUI-test packages"
apt-get install -y \
  sudo ca-certificates curl wget gnupg git git-lfs build-essential pkg-config \
  cmake ninja-build jq ripgrep fd-find file tree less vim unzip zip rsync \
  python3 python3-pip python3-venv pipx nodejs npm openjdk-21-jdk \
  xvfb xauth x11-utils dbus-x11 weston mesa-utils xdg-utils \
  qemu-guest-agent spice-vdagent

log "Ensuring passwordless sudo"
printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$CODEX_USER" >"/etc/sudoers.d/90-${CODEX_USER}"
chmod 0440 "/etc/sudoers.d/90-${CODEX_USER}"
visudo -cf "/etc/sudoers.d/90-${CODEX_USER}" >/dev/null

log "Preparing workspace"
install -d -m 0755 -o "$CODEX_USER" -g "$(id -gn "$CODEX_USER")" "$WORKSPACE"
if command -v fdfind >/dev/null 2>&1; then
  ln -sf /usr/bin/fdfind /usr/local/bin/fd
fi

cat >/usr/local/bin/codex-workspace-mount <<EOF2
#!/usr/bin/env bash
set -euo pipefail
MOUNTPOINT="$WORKSPACE"
TAG="$VIRTIOFS_TAG"
mkdir -p "\$MOUNTPOINT"
if findmnt -rn -T "\$MOUNTPOINT" -t virtiofs >/dev/null 2>&1; then
  echo "Already mounted: \$MOUNTPOINT"
  exit 0
fi
mount -t virtiofs "\$TAG" "\$MOUNTPOINT"
echo "Mounted \$TAG at \$MOUNTPOINT"
EOF2
chmod 0755 /usr/local/bin/codex-workspace-mount

cat >/usr/local/bin/codex-workspace-unmount <<EOF2
#!/usr/bin/env bash
set -euo pipefail
MOUNTPOINT="$WORKSPACE"
if findmnt -rn -T "\$MOUNTPOINT" -t virtiofs >/dev/null 2>&1; then
  umount "\$MOUNTPOINT"
  echo "Unmounted \$MOUNTPOINT"
else
  echo "Not mounted: \$MOUNTPOINT"
fi
EOF2
chmod 0755 /usr/local/bin/codex-workspace-unmount

log "Installing/updating Codex CLI"
npm install -g @openai/codex@latest

systemctl enable --now qemu-guest-agent.service || true

log "Guest toolchain ready"
codex --version || true
