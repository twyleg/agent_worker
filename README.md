# Disposable, headless Codex Xubuntu VM

This bundle creates a disposable **Xubuntu 26.04 LTS Minimal** virtual machine on an **Arch Linux** host using QEMU/KVM + libvirt.

The important properties are:

- Xubuntu installation is fully unattended using Ubuntu's current `autoinstall`/cloud-init mechanism.
- The guest has a lightweight Xfce desktop, so graphical applications can still be launched and tested.
- Codex CLI and common development/GUI-test tooling are installed automatically.
- The `codex` user has passwordless `sudo` inside the disposable VM.
- The host does **not** share its home directory, SSH keys, or complete workspace.
- `share-project.sh` exposes exactly one selected host project via virtiofs and mounts it automatically inside the VM.
- The Xubuntu ISO is cached, so destroying/recreating the VM does not redownload it.

## Files

- `create-codex-vm.sh` — complete zero-touch creation/install/provisioning.
- `share-project.sh` — attach/detach one host project at runtime.
- `destroy-codex-vm.sh` — destroy the VM and virtual disk, retaining reusable cached files.
- `setup-codex-guest.sh` — optional repair/update helper; not needed during normal creation.

## 1. Create the VM

```bash
chmod +x *.sh
./create-codex-vm.sh
```

The script will:

1. install missing Arch packages for libvirt/QEMU;
2. enable the normal libvirt NAT network;
3. download the current Xubuntu 26.04 Minimal ISO if it is not cached;
4. verify it against the official `SHA256SUMS`;
5. generate a dedicated SSH key for this VM;
6. generate an autoinstall configuration;
7. install Xubuntu with no installer interaction;
8. create the `codex` account;
9. install development and GUI-test tools plus Codex CLI;
10. boot the installed VM;
11. wait for cloud-init to finish; and
12. print the VM's IP, GUI password, and SSH command.

Defaults:

```text
VM name        codex-xubuntu
RAM            12 GiB
vCPUs          6
virtual disk   64 GiB qcow2
network        libvirt NAT
GUI            Xfce + SPICE/virtio video
user           codex
keyboard       German
locale         en_US.UTF-8
timezone       Europe/Berlin
```

Override settings through environment variables:

```bash
RAM_MB=8192 VCPUS=4 DISK_GB=50 ./create-codex-vm.sh
```

Other useful overrides include:

```bash
TIMEZONE=Europe/Berlin
LOCALE=de_DE.UTF-8
KEYBOARD_LAYOUT=de
GUEST_HOSTNAME=codex-vm
```

Recreate the disposable VM:

```bash
./create-codex-vm.sh --recreate
```

The cached ISO and dedicated SSH key are retained.

## 2. Access the VM

At the end of creation the script prints an SSH command. The dedicated key is kept under:

```text
~/.local/state/codex-vm/id_ed25519
```

No host SSH key is forwarded or copied into the guest.

For a graphical session:

```bash
sudo virt-viewer --connect qemu:///system codex-xubuntu
```

The random local GUI password is stored with user-only permissions at:

```text
~/.local/state/codex-vm/codex-xubuntu.password
```

SSH password authentication is disabled; SSH uses the dedicated key.

## 3. Authenticate Codex

The CLI itself is already installed. The only intentionally interactive step is authenticating your ChatGPT account:

```bash
codex
```

Choose **Sign in with ChatGPT**.

That authentication lives inside the VM, not on the host.

## 4. Share exactly one project

Nothing from your host workspace is visible initially.

Attach a project:

```bash
./share-project.sh attach ~/dev/workspace/my-project
```

The helper:

1. bind-mounts only that project into `/var/lib/libvirt/codex-share/current` on the host;
2. hot-plugs that directory into the VM as a virtiofs device; and
3. uses the dedicated VM SSH key to mount it automatically at:

```text
/home/codex/workspace
```

Then in the VM:

```bash
cd ~/workspace
codex
```

Check the current share:

```bash
./share-project.sh status
```

Detach it:

```bash
./share-project.sh detach
```

The helper first unmounts the filesystem inside the guest, then removes the virtiofs device and host bind mount.

### UID/GID note

Virtiofs passthrough preserves numeric Unix ownership. This setup is simplest when your normal Arch user is UID/GID `1000:1000`, because the first Xubuntu user (`codex`) normally receives the same IDs. The creation script warns if the host IDs differ.

## 5. GUI testing

The VM is installed headlessly, but it is **not a headless-only system**. Xubuntu/Xfce, SPICE integration, X11 and Wayland test tooling are installed.

Normal graphical application:

```bash
./your-gui-application
```

Headless X11 test:

```bash
xvfb-run -a ./your-test-command
```

Weston is installed for tests that specifically need a Wayland compositor.

## 6. Destroy/recreate

```bash
./destroy-codex-vm.sh
./create-codex-vm.sh
```

The VM disk is removed. The downloaded Xubuntu ISO and the dedicated SSH key are retained.

## Installed development tooling

The base VM includes, among other tools:

- Git + Git LFS
- build-essential, CMake, Ninja, pkg-config
- Python 3, venv, pip, pipx
- Node.js + npm
- current Codex CLI from `@openai/codex`
- OpenJDK 21
- jq, ripgrep, fd, rsync, zip/unzip
- Xvfb + X11 utilities
- Weston
- Mesa utilities
- QEMU guest agent + SPICE guest agent

Project-specific dependencies can simply be installed by Codex using passwordless `sudo` because the VM is intentionally disposable.

## Troubleshooting installation

The unattended installer writes progress to the VM serial console:

```bash
sudo virsh console codex-xubuntu
```

Exit a `virsh console` session with `Ctrl+]`.

The graphical installer/session can also be inspected at any point:

```bash
sudo virt-viewer --connect qemu:///system codex-xubuntu
```

After installation, inspect first-boot provisioning with:

```bash
sudo cloud-init status --long
sudo journalctl -u cloud-final
```
