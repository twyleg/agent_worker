#!/usr/bin/env bash
set -euo pipefail

VM_NAME="${VM_NAME:-codex-xubuntu}"
LIBVIRT_URI="${LIBVIRT_URI:-qemu:///system}"
IMAGE_DIR="${IMAGE_DIR:-/var/lib/libvirt/images/codex-vm}"
DISK_PATH="$IMAGE_DIR/${VM_NAME}.qcow2"
EXPORT_DIR="${CODEX_EXPORT_DIR:-/var/lib/libvirt/codex-share/current}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/codex-vm"
STATE_XML="$STATE_DIR/current-share.xml"
STATE_PATH="$STATE_DIR/current-share.path"

if sudo virsh -c "$LIBVIRT_URI" dominfo "$VM_NAME" >/dev/null 2>&1; then
  printf "Destroy VM '%s' and delete %s? [y/N] " "$VM_NAME" "$DISK_PATH"
  read -r answer
  [[ "$answer" =~ ^[Yy]$ ]] || exit 0

  if sudo virsh -c "$LIBVIRT_URI" domstate "$VM_NAME" | grep -qviE 'shut off|shutoff'; then
    sudo virsh -c "$LIBVIRT_URI" destroy "$VM_NAME" >/dev/null || true
  fi
  sudo virsh -c "$LIBVIRT_URI" undefine "$VM_NAME" --nvram >/dev/null 2>&1 \
    || sudo virsh -c "$LIBVIRT_URI" undefine "$VM_NAME" >/dev/null
else
  echo "VM '$VM_NAME' is not defined; cleaning any leftover disk/share state."
fi

if findmnt -rn -M "$EXPORT_DIR" >/dev/null 2>&1; then
  sudo umount "$EXPORT_DIR" || sudo umount -l "$EXPORT_DIR" || true
fi
rm -f "$STATE_XML" "$STATE_PATH"
sudo rm -f "$DISK_PATH"

echo "Removed '$VM_NAME'."
echo "Kept the downloaded Xubuntu ISO and dedicated VM SSH key for fast recreation."
