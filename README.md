# Disposable, headless agent Xubuntu VM

This bundle creates a disposable **Xubuntu 26.04 LTS Minimal** virtual machine on an **Arch Linux** host using QEMU/KVM + libvirt.

The VM identity and sharing paths use agent-neutral names. Codex CLI is currently the agent installed by provisioning; other agents can be installed inside the guest.

The important properties are:

- Xubuntu installation is fully unattended using Ubuntu's current `autoinstall`/cloud-init mechanism.
- The guest has a lightweight Xfce desktop, so graphical applications can still be launched and tested.
- Codex CLI and common development/GUI-test tooling are installed automatically.
- The `agent` user has passwordless `sudo` inside the disposable VM.
- The host does **not** share its home directory, SSH keys, or complete workspace.
- `agent_vm_mount_project` exposes exactly one selected host project via virtiofs and mounts it automatically inside the VM.
- The Xubuntu ISO is cached, so destroying/recreating the VM does not redownload it.

## Files

- `scripts/agent_vm_create` — complete zero-touch creation/install/provisioning.
- `scripts/agent_vm_mount_project` — attach/detach one host project at runtime.
- `scripts/agent_vm_destroy` — destroy the VM and virtual disk, retaining reusable cached files.
- `scripts/agent_vm_setup_guest` — optional repair/update helper; not needed during normal creation.
- `scripts/agent_vm_status` — VM state, resources, guest IP and attached project.
- `scripts/agent_vm_start` — boot an existing VM; succeeds if it is already running.
- `scripts/agent_vm_shutdown` — request graceful shutdown from the host; `agent_vm_stutdown` is an alias.
- `scripts/agent_vm_connect` — SSH using the dedicated key and discovered guest IP.
- `scripts/agent_vm_codex_usage` — live Codex usage percentages and reset times for the guest's ChatGPT account.
- `activate` / `activate.fish` — source to add `scripts/` to the current shell's `PATH`.

## Local data directory

Runtime files default to `.data/` at the project root, regardless of the shell's
current directory:

```text
.data/
  images/
    agent-xubuntu.qcow2
    iso/
  state/
    id_ed25519
    id_ed25519.pub
    known_hosts
    agent-xubuntu.password
    autoinstall/
    current-share.xml
    current-share.path
  share/
    current/
```

The state directory is private to your user, and `.data/` is ignored by Git.
`share/current` is a bind mount while a project is attached; it is not a copy.
Detach the project before moving this directory.

Set `DATA_DIR=/absolute/path` consistently for creation, sharing and destruction
to relocate the whole layout. `IMAGE_DIR`, `STATE_DIR` and `AGENT_EXPORT_DIR`
remain available for individual overrides. Libvirt still manages the domain
configuration, UEFI state and temporary installation files separately.

QEMU/libvirt must have permission to traverse the parent directories and access
the disk and ISO. A private home directory can block access even when `.data/`
itself is readable. If libvirt reports a path access error, grant the configured
QEMU account directory traversal using ACLs rather than making your home contents
readable, or choose an accessible `DATA_DIR`.

Existing VMs are not moved automatically. For a VM created with the previous
agent defaults, keep using its original locations by setting:

```bash
export IMAGE_DIR=/var/lib/libvirt/images/agent-vm
export STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/agent-vm"
export AGENT_EXPORT_DIR=/var/lib/libvirt/agent-share/current
```

## 1. Create the VM

From the project root, activate the commands in your current shell:

```bash
source ./activate
```

For Fish:

```fish
source ./activate.fish
```

Both add this project's `scripts/` directory to `PATH`, so commands can be run
from any working directory. Repeated activation does not duplicate the entry.
Activation changes only the current shell's environment. You can also invoke
`scripts/agent_vm_create` directly without activation.

```bash
agent_vm_create
```

Run creation as your normal user. It validates sudo authorization once at startup
(asking for your password if needed), then refreshes the cached authorization
every 30 seconds while installation runs. Later privileged commands use
noninteractive sudo, and the refresh process stops when the script exits. You
can leave the installation unattended after the initial authorization; no root
shell or root PATH configuration is needed. This relies on sudo timestamp caching
being enabled; a policy that disables caching or revokes authorization will still
cause privileged operations to fail.

The script will:

1. install missing Arch packages for libvirt/QEMU;
2. enable the normal libvirt NAT network;
3. download the current Xubuntu 26.04 Minimal ISO if it is not cached;
4. verify it against the official `SHA256SUMS`;
5. generate a dedicated SSH key for this VM;
6. generate an autoinstall configuration;
7. install Xubuntu with no installer interaction;
8. create the `agent` account;
9. install development and GUI-test tools plus Codex CLI;
10. boot the installed VM;
11. wait for cloud-init to finish; and
12. print the VM's IP, GUI password, and SSH command.

Defaults:

```text
VM name        agent-xubuntu
RAM            12 GiB
vCPUs          6
virtual disk   64 GiB qcow2
network        libvirt NAT
GUI            Xfce + SPICE/virtio video
user           agent
keyboard       German
locale         en_US.UTF-8
timezone       Europe/Berlin
```

Override settings through environment variables:

```bash
RAM_MB=8192 VCPUS=4 DISK_GB=50 agent_vm_create
```

Other useful overrides include:

```bash
TIMEZONE=Europe/Berlin
LOCALE=de_DE.UTF-8
KEYBOARD_LAYOUT=de
GUEST_HOSTNAME=agent-vm
AGENT_USER=agent
```

Recreate the disposable VM:

```bash
agent_vm_create --recreate
```

The cached ISO and dedicated SSH key are retained.

### Existing VMs created with the Codex defaults

Changing these defaults does not rename an existing VM or its guest user. To use
these helpers with the existing `codex-xubuntu` VM, set the following in your host
shell before running sharing or destruction commands:

```bash
export VM_NAME=codex-xubuntu
export AGENT_USER=codex
export IMAGE_DIR=/var/lib/libvirt/images/codex-vm
export STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/codex-vm"
export AGENT_EXPORT_DIR=/var/lib/libvirt/codex-share/current
export AGENT_SHARE_TAG=codex-project
export WORKSPACE_HELPER_PREFIX=codex-workspace
```

For the optional repair helper, set `AGENT_USER=codex`,
`AGENT_SHARE_TAG=codex-project` and `WORKSPACE_HELPER_PREFIX=codex-workspace`
inside the existing guest, for example:

```bash
sudo env AGENT_USER=codex AGENT_SHARE_TAG=codex-project \
  WORKSPACE_HELPER_PREFIX=codex-workspace bash /path/to/agent_vm_setup_guest
```

Legacy `CODEX_USER`, `CODEX_EXPORT_DIR` and `CODEX_SHARE_TAG` environment
variables remain supported; the corresponding `AGENT_*` variables take priority.

## 2. Access the VM

After activation, manage the VM from the host:

```bash
agent_vm_status
agent_vm_start
agent_vm_connect
agent_vm_connect codex --version
agent_vm_codex_usage
agent_vm_codex_usage --json
agent_vm_shutdown
```

Shutdown requests a graceful power-off without deleting the VM or disk. Use
`agent_vm_status` to check when it finishes. `agent_vm_stutdown` also works.
These commands honor `VM_NAME`, `LIBVIRT_URI`, `DATA_DIR`, `STATE_DIR` and
`AGENT_USER`, including the existing-VM overrides above.

Connection, status and Codex usage queries use libvirt's read-only connection
without sudo. If guest-agent queries are unavailable on that connection, IP
discovery falls back to the default network's DHCP leases. The host must allow
read-only libvirt access; test with
`virsh --readonly -c qemu:///system dominfo agent-xubuntu`.
Start and shutdown still use sudo for management operations.

`agent_vm_codex_usage` queries the documented
[`account/rateLimits/read` app-server method](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt)
inside the VM. It uses the guest's existing Codex ChatGPT login, requires Codex
and Python 3 in the guest, and reports used/remaining percentages and reset times
in the guest's local timezone. Credentials stay inside the VM. No inference turn
is started. Missing quota windows are shown as unavailable; API-key-only login
does not provide the ChatGPT plan limits. The query times out after 30 seconds.

At the end of creation the script prints an SSH command. The dedicated key is kept under:

```text
.data/state/id_ed25519
```

No host SSH key is forwarded or copied into the guest.

For a graphical session:

```bash
sudo virt-viewer --connect qemu:///system agent-xubuntu
```

The random local GUI password is stored with user-only permissions at:

```text
.data/state/agent-xubuntu.password
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
agent_vm_mount_project attach ~/dev/workspace/my-project
```

The helper:

1. bind-mounts only that project into `.data/share/current` on the host;
2. hot-plugs that directory into the VM as a virtiofs device; and
3. uses the dedicated VM SSH key to mount it automatically under the selected project's directory name:

```text
/home/agent/workspace/my-project
```

Then in the VM:

```bash
cd ~/workspace/my-project
codex
```

Check the current share:

```bash
agent_vm_mount_project status
```

Detach it:

```bash
agent_vm_mount_project detach
```

The helper first unmounts the project directory inside the guest, then removes the virtiofs device and host bind mount. Mounting works with existing guests without updating their helper scripts. If a previous attach attempt failed after hot-plugging the device, run `agent_vm_mount_project detach` before attaching again.

IP discovery skips loopback and link-local IPv4 addresses reported by the guest agent and falls back to libvirt DHCP leases.

### UID/GID note

Virtiofs passthrough preserves numeric Unix ownership. This setup is simplest when your normal Arch user is UID/GID `1000:1000`, because the first Xubuntu user (`agent`) normally receives the same IDs. The creation script warns if the host IDs differ.

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
agent_vm_destroy
agent_vm_create
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

Project-specific dependencies can simply be installed by an agent using passwordless `sudo` because the VM is intentionally disposable.

## Troubleshooting installation

The unattended installer writes progress to the VM serial console:

```bash
sudo virsh console agent-xubuntu
```

Exit a `virsh console` session with `Ctrl+]`.

The graphical installer/session can also be inspected at any point:

```bash
sudo virt-viewer --connect qemu:///system agent-xubuntu
```

After installation, inspect first-boot provisioning with:

```bash
sudo cloud-init status --long
sudo journalctl -u cloud-final
```
