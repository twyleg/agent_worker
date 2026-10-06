#!/usr/bin/env bash
set -euo pipefail

# Hot-plug exactly one host project into the running Codex VM via virtiofs.
# The helper also mounts/unmounts the filesystem inside the guest over the
# dedicated SSH key created by create-codex-vm.sh.
#
# Usage:
#   ./share-project.sh attach /path/to/project
#   ./share-project.sh status
#   ./share-project.sh detach

VM_NAME="${VM_NAME:-codex-xubuntu}"
CODEX_USER="${CODEX_USER:-codex}"
LIBVIRT_URI="${LIBVIRT_URI:-qemu:///system}"
TAG="${CODEX_SHARE_TAG:-codex-project}"
EXPORT_DIR="${CODEX_EXPORT_DIR:-/var/lib/libvirt/codex-share/current}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/codex-vm"
STATE_XML="$STATE_DIR/current-share.xml"
STATE_PATH="$STATE_DIR/current-share.path"
SSH_KEY="$STATE_DIR/id_ed25519"
KNOWN_HOSTS="$STATE_DIR/known_hosts"

log() { printf '==> %s\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

mkdir -p "$STATE_DIR"

vm_running() {
  sudo virsh -c "$LIBVIRT_URI" domstate "$VM_NAME" 2>/dev/null | grep -qi '^running$'
}

export_is_mounted() {
  findmnt -rn -M "$EXPORT_DIR" >/dev/null 2>&1
}

vm_mac() {
  sudo virsh -c "$LIBVIRT_URI" domiflist "$VM_NAME" \
    | awk '$2 == "network" {print $5; exit}'
}

vm_ip() {
  local ip mac
  ip="$(sudo virsh -c "$LIBVIRT_URI" domifaddr "$VM_NAME" --source agent 2>/dev/null \
        | awk '/ipv4/ {sub(/\/.*/, "", $4); print $4; exit}')"
  if [[ -n "$ip" ]]; then
    printf '%s\n' "$ip"
    return 0
  fi
  mac="$(vm_mac)"
  ip="$(sudo virsh -c "$LIBVIRT_URI" net-dhcp-leases default 2>/dev/null \
        | awk -v m="$mac" 'tolower($3)==tolower(m) {sub(/\/.*/, "", $5); print $5; exit}')"
  [[ -n "$ip" ]] && printf '%s\n' "$ip"
}

guest_ssh() {
  local ip
  [[ -f "$SSH_KEY" ]] || return 1
  ip="$(vm_ip || true)"
  [[ -n "$ip" ]] || return 1
  ssh -i "$SSH_KEY" \
    -o BatchMode=yes \
    -o ConnectTimeout=5 \
    -o StrictHostKeyChecking=accept-new \
    -o "UserKnownHostsFile=$KNOWN_HOSTS" \
    "${CODEX_USER}@${ip}" "$@"
}

write_xml() {
  cat >"$STATE_XML" <<EOF2
<filesystem type='mount' accessmode='passthrough'>
  <driver type='virtiofs' queue='1024'/>
  <source dir='$EXPORT_DIR'/>
  <target dir='$TAG'/>
</filesystem>
EOF2
}

case "${1:-}" in
  attach)
    [[ $# -eq 2 ]] || die "Usage: $0 attach PROJECT_PATH"
    vm_running || die "VM '$VM_NAME' is not running."
    [[ ! -f "$STATE_XML" ]] || die "A project is already attached. Detach it first: $0 detach"

    project="$(realpath -e "$2")"
    [[ -d "$project" ]] || die "Not a directory: $project"

    sudo install -d -m 0755 "$EXPORT_DIR"
    export_is_mounted && die "$EXPORT_DIR is already a mount point. Clean it up before attaching another project."

    log "Staging only this project: $project"
    sudo mount --bind "$project" "$EXPORT_DIR"

    write_xml
    printf '%s\n' "$project" >"$STATE_PATH"

    log "Hot-attaching project to '$VM_NAME'"
    if ! sudo virsh -c "$LIBVIRT_URI" attach-device "$VM_NAME" "$STATE_XML" --live; then
      sudo umount "$EXPORT_DIR" || true
      rm -f "$STATE_XML" "$STATE_PATH"
      die "virtiofs hot-attach failed."
    fi

    log "Mounting project inside the guest"
    if guest_ssh 'sudo /usr/local/bin/codex-workspace-mount'; then
      echo "Ready: $project -> /home/${CODEX_USER}/workspace"
    else
      cat <<EOF2
The virtiofs device is attached, but automatic guest mounting failed.
Inside the VM run:
  sudo codex-workspace-mount

The project will then be available at:
  /home/${CODEX_USER}/workspace
EOF2
    fi
    ;;

  detach)
    vm_running || die "VM '$VM_NAME' is not running."
    [[ -f "$STATE_XML" ]] || die "No tracked project share is attached."

    log "Unmounting project inside the guest"
    if ! guest_ssh 'sudo /usr/local/bin/codex-workspace-unmount'; then
      die "Could not unmount the project inside the guest. Ensure it is not in use, then retry."
    fi

    log "Hot-detaching $(cat "$STATE_PATH" 2>/dev/null || echo project)"
    sudo virsh -c "$LIBVIRT_URI" detach-device "$VM_NAME" "$STATE_XML" --live

    export_is_mounted && sudo umount "$EXPORT_DIR"
    rm -f "$STATE_XML" "$STATE_PATH"
    log "Detached; no host project is exposed to the VM"
    ;;

  status)
    if [[ -f "$STATE_PATH" ]]; then
      echo "Host project:  $(cat "$STATE_PATH")"
      echo "Staging dir:   $EXPORT_DIR"
      echo "Guest path:    /home/${CODEX_USER}/workspace"
      export_is_mounted && findmnt "$EXPORT_DIR"
      if guest_ssh 'findmnt -rn -T /home/codex/workspace -t virtiofs >/dev/null && echo "Guest mount:   mounted" || echo "Guest mount:   not mounted"' 2>/dev/null; then
        :
      fi
    else
      echo "No project is currently attached."
    fi
    ;;

  *)
    echo "Usage: $0 {attach PROJECT_PATH|detach|status}" >&2
    exit 2
    ;;
esac
