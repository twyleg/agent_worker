# Agent Worker

Manage a disposable **Xubuntu 26.04 LTS Minimal** VM on an **Arch Linux** host
using QEMU/KVM and libvirt. The guest gives Codex full control of an isolated
development environment, with a lightweight Xfce desktop and a persistent tmux
workspace. Selected host projects can be attached independently using virtiofs.
The host's home directory and SSH keys are never shared automatically.

The management implementation is Python. Its application structure follows
[twyleg/template_project_python](https://github.com/twyleg/template_project_python),
using `simple-python-app==0.4.1` and its `SubcommandApplication` command handlers,
logging and configuration infrastructure. It uses the template's `setup.py`/`setup.cfg`,
Versioneer, requirements, tox, Black, mypy, pytest, Sphinx, PyInstaller and GitHub
Actions structure. Python 3.11 or newer is required.

## Installation

Install for your user from this checkout:

```bash
python3 -m pip install --user .
```

Make sure `~/.local/bin` is on your `PATH`. On systems that restrict installation
into the system Python, use pipx or a virtual environment:

```bash
pipx install .
```

For development:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .
```

The main command is `agent_worker`; `python -m agent_worker` provides the same
interface. Each action and subaction has `--help`.

## Shell completion

Completion covers subcommands, option names, fixed choices, directories and
attached project names/paths for `project detach`. It uses the parser directly,
so new commands and options become available automatically. Project suggestions
read local records and honor `--config`, `--data-dir` and environment settings;
completion does not connect to the VM or initialize logging.

Builds include completion files for Bash, Zsh and Fish. With **pipx 1.16.0 or
newer**, installation exposes them automatically under `~/.local/share/`.
With a default `pip install --user`, they are installed there directly. Fish finds
`~/.local/share/fish/vendor_completions.d/agent_worker.fish` automatically, so no
manual `source` or configuration file is required.

Bash uses its `bash-completion` framework to discover the installed file. Zsh
needs `~/.local/share/zsh/site-functions` on `fpath` before `compinit`.

For older pipx versions or pip installs inside a virtual environment, use the
following manual setup. Bash (`~/.bashrc`):

```bash
eval "$(agent_worker completion bash)"
```

Zsh, after `compinit` in `~/.zshrc`:

```zsh
eval "$(agent_worker completion zsh)"
```

Fish, for the current session:

```fish
agent_worker completion fish | source
```

To persist manual Fish setup:

```fish
mkdir -p ~/.config/fish/completions
agent_worker completion fish > ~/.config/fish/completions/agent_worker.fish
```

Type a space after `agent_worker` before pressing Tab to complete subcommands.
In Fish, `complete -C 'agent_worker '` should list commands such as `vm` and
`project`. The completion script queries the current parser on each Tab press,
so new commands and options appear after upgrades without regenerating your
shell configuration. Remote arguments following `vm ssh` do not receive
remote-shell completion.

## Workflow

```bash
agent_worker vm create
agent_worker vm start
agent_worker project attach ~/dev/my-project
agent_worker vm connect
```

Creation already boots the new VM. `start` is useful for subsequent sessions and
succeeds when it is already running. On the first connection, authenticate Codex
with your ChatGPT account inside the VM. Authentication stays in the guest.

A single attached project becomes the starting directory of a new workspace;
with multiple projects, use `cd ~/workspace/<project-name>` in Codex or the shell.

```bash
agent_worker vm status
agent_worker project status
agent_worker codex usage
agent_worker codex usage --json
agent_worker vm shutdown
```

Shutdown requests a graceful power-off and preserves the VM and disk. Check
`vm status` for completion.

## Commands

| Command | Purpose |
| --- | --- |
| `agent_worker vm create [--recreate]` | Unattended installation and first-boot provisioning |
| `agent_worker vm start` | Boot the VM |
| `agent_worker vm shutdown` | Request graceful shutdown |
| `agent_worker vm status` | Show domain details, resources, IP and projects |
| `agent_worker vm connect` | Attach to the persistent Codex/tmux workspace |
| `agent_worker vm ssh [--tty] [COMMAND ...]` | Plain SSH shell or remote command |
| `agent_worker vm destroy [--yes]` | Remove the VM and disk; keep ISO and SSH key |
| `agent_worker project attach PATH [NAME]` | Attach a selected host directory |
| `agent_worker project detach [PATH_OR_NAME \| --all]` | Detach one or all projects |
| `agent_worker project status` | Show tracked project exports |
| `agent_worker codex usage [--json]` | Query the guest's live Codex account rate limits |
| `agent_worker guest setup` | Repair/update tooling, run as root **inside the guest** |

Examples of plain SSH commands:

```bash
agent_worker vm ssh
agent_worker vm ssh codex --version
agent_worker vm ssh --tty bash
agent_worker vm ssh -- python3 - --help < script.py
```

Remote arguments follow SSH's normal command-string semantics. Quote a complete
remote command when it contains shell syntax or arguments with spaces:

```bash
agent_worker vm ssh 'cd "$HOME/workspace/my project" && git status'
```


## Configuration

No configuration file is required. Settings resolve in this order, from highest
to lowest priority:

1. Explicit CLI options.
2. Existing environment variables (including the legacy `CODEX_*` aliases).
3. JSON application configuration.
4. Defaults.

Global options go **before** the action:

```bash
agent_worker --data-dir /srv/agent-worker --vm-name my-agent vm status
agent_worker --config agent_worker_config.json vm create --ram-mb 8192 --vcpus 4 --disk-gb 50
```

The framework loads `agent_worker_config.json` or `.agent_worker_config.json`
from the current directory, then your home directory. The root configuration is
the editable template-style starting point; a packaged empty configuration supplies
defaults when no user file exists. An explicit missing file or a configuration that
does not match the packaged JSON schema is an error. Relative paths resolve against
the current directory. Unknown keys are rejected.

| JSON setting | Environment variable | Default |
| --- | --- | --- |
| `vm_name` | `VM_NAME` | `agent-xubuntu` |
| `libvirt_uri` | `LIBVIRT_URI` | `qemu:///system` |
| `agent_user` | `AGENT_USER` / `CODEX_USER` | `agent` |
| `data_dir` | `DATA_DIR` | See runtime data below |
| `state_dir` | `STATE_DIR` | `<data_dir>/state` |
| `image_dir` | `IMAGE_DIR` | `<data_dir>/images` |
| `export_dir` | `AGENT_EXPORT_DIR` / `CODEX_EXPORT_DIR` | `<data_dir>/share/current` (old staging mounts only) |
| `share_tag` | `AGENT_SHARE_TAG` / `CODEX_SHARE_TAG` | `agent-project` (legacy share only) |
| `workspace_helper_prefix` | `WORKSPACE_HELPER_PREFIX` | `agent-workspace` |
| `ram_mb` | `RAM_MB` | `12288` |
| `vcpus` | `VCPUS` | `6` |
| `disk_gb` | `DISK_GB` | `64` |
| `xubuntu_series` | `XUBUNTU_SERIES` | `26.04` |
| `timezone` | `TIMEZONE` | `Europe/Berlin` |
| `locale` | `LOCALE` | `en_US.UTF-8` |
| `keyboard_layout` | `KEYBOARD_LAYOUT` | `de` |
| `keyboard_variant` | `KEYBOARD_VARIANT` | empty |
| `guest_hostname` | `GUEST_HOSTNAME` | `agent-worker` |
| `ready_timeout` | `READY_TIMEOUT` | `900` seconds for SSH readiness |
| `resource_dir` | `RESOURCE_DIR` | Packaged resources |

The `AGENT_*` names take priority over `CODEX_*`. Create also accepts `--ram-mb`,
`--vcpus` and `--disk-gb`; global `--libvirt-uri` and `--resource-dir` are available.
`--verbose`, `--logging-config` and `--logging-dir` use the framework's logging
options. The root `logging.yaml` uses the template's console and file handlers;
console warnings/errors go to stderr, keeping JSON stdout usable in pipelines.
A packaged logging configuration supports installed use. Logs default to
`${XDG_STATE_HOME:-~/.local/state}/agent-worker/log`.

## Runtime data and migration

Running from a checkout, including an editable install, retains the original
`<checkout>/.data` default, independent of the shell's current directory. A normal
wheel/pip install defaults to `${XDG_DATA_HOME:-~/.local/share}/agent-worker`.
Runtime files never go into the installed package directory.

To manage an existing VM after switching to a normal pip installation, explicitly
keep the checkout's data path:

```bash
export DATA_DIR=/absolute/path/to/agent_worker/.data
agent_worker vm status
```

Share records retain their existing format. New runtime data has this layout:

```text
<data_dir>/
  images/
    agent-xubuntu.qcow2
    iso/
  state/
    id_ed25519
    id_ed25519.pub
    known_hosts
    agent-xubuntu.password
    autoinstall/
    shares/<project-id>/
      project.path
      project.name
      export.path
      device.tag
      device.xml
```

State, SSH keys and autoinstall files are private to your user. New shares export
the original project directory directly through virtiofs. Runtime data contains
share records, not project copies, symlinks or host bind mounts. Each record's
`export.path` and `device.xml` point to the original project directory.

Older versions created **writable host bind mounts** at
`share/current.d/<project-id>` (or the legacy `share/current`). **Deleting files
inside these mounted directories deletes the original project files. VM shutdown
does not unmount these old host mounts.** Existing records remain readable for
status and detach, but attach requires detaching and reattaching them to switch
to direct exports. No new host bind mounts are created, including after restart.

`IMAGE_DIR` and `STATE_DIR` overrides still work. `AGENT_EXPORT_DIR` now identifies
old staging locations for cleanup only. Libvirt manages the domain definition,
UEFI state and temporary installer files separately.

Before moving or deleting runtime data from an older installation:

1. While the VM is running, run `agent_worker project detach --all`. This also
   works after the VM is fully shut off; it then skips guest access. If cleanup
   fails, resolve the error and retry. Tracking is retained for failed shares.
2. Confirm shutdown has completed with `agent_worker vm status` before moving
   runtime data or manually unmounting leftover staging paths.
3. Inspect the host mount table, including both old and new data directory names:

   ```bash
   findmnt -rn -o TARGET,SOURCE,FSTYPE | rg 'agent-worker(-vm)?/share/'
   ```

   For custom export locations, inspect those paths too. If records are gone or
   the directory has already moved, manually unmount each exact staging target
   shown in the host mount table, for example:

   ```bash
   sudo umount -- "$HOME/.local/share/agent-worker/share/current.d/PROJECT_ID"
   ```

   If unmount reports busy, close processes using that staging path and retry.
   Do not use recursive deletion, forced unmount or lazy unmount as cleanup.
   Recheck the mount table after unmounting. Empty staging directories may then
   be removed with `rmdir`, which refuses to delete nonempty directories.

For an existing VM, moving runtime data also requires updating the absolute disk
and other file paths in its libvirt definition (`sudo virsh -c qemu:///system edit
agent-xubuntu`). Check `sudo virsh -c qemu:///system dumpxml agent-xubuntu
--inactive` for references to the old location, and adjust the command if you use
a different domain or URI. A symlink from a checkout's `.data` to the new location
does not update libvirt or old share records. Detach old shares before moving;
reattach projects after the VM starts at its new location. Recreating the VM is
not required just to change these paths.

If recursive deletion has already reached project files, stop deleting and
minimize writes to the affected filesystem before attempting recovery. Restore
backups or remote repositories to a separate filesystem; uncommitted files need
separate recovery. Unmounting leftovers does not restore deleted files.

For VMs created with earlier defaults, keep their original identity and paths:

```bash
export VM_NAME=codex-xubuntu
export AGENT_USER=codex
export IMAGE_DIR=/var/lib/libvirt/images/codex-vm
export STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/codex-vm"
export AGENT_EXPORT_DIR=/var/lib/libvirt/codex-share/current
export AGENT_SHARE_TAG=codex-project
export WORKSPACE_HELPER_PREFIX=codex-workspace
```

## Creation and provisioning

Run `agent_worker vm create` as your normal user on the Arch host. It requires KVM
(`/dev/kvm`) and validates sudo authorization once, then refreshes it every 30
seconds. Subsequent creation commands use noninteractive sudo; the refresh stops
on completion, errors or interruption. This requires enabled sudo timestamp
caching.

Creation installs the Arch host tools, enables libvirt's default NAT network,
locates and downloads the latest Xubuntu Minimal ISO for the selected series,
verifies the official SHA-256 checksum, creates a dedicated SSH key, generates
cloud-init/autoinstall data, and runs `virt-install` unattended. It boots the
installed VM and waits for SSH and cloud-init before printing connection details
and the random GUI/console password.

The guest has UEFI, virtio devices, SPICE graphics and a serial console. It installs
Xfce, development and GUI-test tools, tmux, the QEMU guest agent, and Codex CLI.
The `agent` user has passwordless sudo. SSH password authentication is disabled.
No host authentication keys are forwarded or copied.

Provisioning resources live in `agent_worker/resources/`:

- `packages_list`: guest apt package names; blank lines and `#` comments are accepted.
- `host_packages_list`: Arch host package names, using the same format.
- `default_network.xml`: fallback NAT network definition when the host has no system-provided definition.
- `tmux.conf` and `tmux.conf.local`: installed at creation and refreshed on connect.
- `codex.config.toml`: Codex defaults with `sandbox_mode = "danger-full-access"`
  and `approval_policy = "never"` inside the disposable guest.

To customize an installed package's provisioning resources, copy these six files
into your own directory and pass `--resource-dir /path/to/resources`. Host packages
and the network definition are used during creation. The guest package list applies
to creation or guest setup. Codex configuration updates preserve
unrelated settings and authentication. New sessions launch Codex with full access;
already running sessions retain their active settings.

Creation refuses to replace an existing VM unless `--recreate` is given:

```bash
agent_worker vm create --recreate
```

Recreation and destruction remove tracked shares and the disk, retaining the
cached ISO and dedicated SSH key. Destruction prompts unless `--yes` is passed.
The optional `sudo agent_worker guest setup` repair command runs inside a guest with
this Python package installed; normal creation requires no separate repair step.

## Persistent tmux workspace

`agent_worker vm connect` uploads standalone Python helpers and resources to
`~/.local/lib/agent-worker` inside the guest. It creates `agent-workspace` with Codex
on the left (70%), a curses dashboard at the upper right, and a shell below it.
The dashboard polls CPU, memory, disk and Codex rate limits every ten seconds,
with utilization bars and a clock that ticks every second. Up/Down or `j`/`k`
scroll, Page Up/Down also work, and `q` closes the dashboard pane. Unavailable
quotas or authentication errors do not stop the dashboard.

Reconnecting preserves running panes and Codex. An existing user-created tmux
session is respected when the managed workspace does not yet exist. Concurrent
connections serialize workspace creation. Tmux configs are copied and reloaded
on every connection, replacing the guest's `.tmux.conf` and `.tmux.conf.local`;
make persistent edits in the resource files. Existing guests install tmux using
passwordless sudo if needed.

Use `Ctrl+a`, then an arrow key to move between panes; `Ctrl+a`, then `c` opens
another window, `Ctrl+h`/`Ctrl+l` switches windows, `z` zooms the current pane,
and `d` detaches. `Ctrl+a` twice switches to the previous window. Use `vm ssh`
for a plain shell without tmux.

Status, SSH and quota discovery use read-only libvirt without sudo. IP discovery
filters loopback and link-local addresses from the guest agent and falls back to
DHCP leases. Test host access with:

```bash
virsh --readonly -c qemu:///system dominfo agent-xubuntu
```

Codex usage queries run the existing app-server rate-limit helper inside the VM,
using its ChatGPT login without starting an inference turn. Credentials remain in
the guest. API-key-only accounts may not expose ChatGPT plan quotas. The helper
times out after 30 seconds and reports missing windows as unavailable.

## Share selected projects

```bash
agent_worker project attach ~/dev/my-project
agent_worker project attach ~/dev/another-project
agent_worker project attach ~/other/my-project my-project-other
agent_worker project status
```

`project status` shows one row per project with host and guest mount states and
the host path. Direct exports show `available` when the host directory exists;
old staging exports show their host mount state. Use `project status --verbose`
(or `-v`) for the full paths and host mount details. States are highlighted in
terminals: green for available/mounted,
red for not mounted, and yellow when the guest cannot be reached. Redirected output
is plain text; `NO_COLOR` disables automatic colors. Use `--color always` or
`--color never` to override color detection.

Each project receives an independent virtiofs device exporting its original host
directory directly and a guest mount at `/home/agent/workspace/<name>`. Directory
names containing spaces are supported. Repeating attach for the same path and name reuses the
record, restoring a missing device and retrying guest mounting.
Duplicate guest names need an explicit alias.

```bash
agent_worker project detach my-project
agent_worker project detach ~/dev/another-project
agent_worker project detach --all
```

With one project, `detach` without a selector works. With multiple projects,
specify a name/path or `--all`. Detach first unmounts the guest, then removes its
live device. For old staging shares it also unmounts the host bind mount. Direct
exports never unmount the project directory, even if it is a filesystem mount
point. Tracking is retained when cleanup fails so the operation can be retried.
When the VM is fully shut off, detach only cleans host staging mounts and records.
Live devices do not persist across VM/host restarts;
repeat each project's `attach` command to restore them. There is no fixed
single-project limit.

Virtiofs preserves numeric UID/GID ownership. The guest's first user is normally
`1000:1000`; creation warns if your host user differs. QEMU must be able to traverse
parent directories and read disk/ISO files. Before installation, creation reads
QEMU's UID/GID from libvirt capabilities and checks storage access as that account.
It grants that UID traversal-only ACLs on inaccessible parent directories and a
read-only ACL on the ISO if needed. This supports private `~/.local/share`
directories without making them world-accessible. The host `acl` package supplies
`setfacl`; no recursive permission changes are made, and the state directory,
SSH keys and autoinstall credentials remain private. Libvirt manages the disk's
ownership and permissions. If ACLs are unsupported on the storage filesystem,
choose an accessible `IMAGE_DIR`.

If an earlier install failed with `Cannot access storage file ... Permission
denied`, rerun `vm create` after this fix. Check `vm status` first: if the failed
attempt left a defined domain, inspect it before choosing `vm create --recreate`,
which deletes its VM disk. OS detection warnings about `generic` are separate
from storage access failures.

## GUI use and troubleshooting

The guest includes Git/LFS, build-essential, CMake, Ninja, Python/venv/pip/pipx,
Node/npm, OpenJDK 21, jq, ripgrep, fd, rsync, Xvfb, Weston, Mesa utilities and SPICE
integration. See `packages_list` for the complete list. Agents can install
project-specific dependencies inside the disposable VM using passwordless sudo.

```bash
# Graphical console on the host
sudo virt-viewer --connect qemu:///system agent-xubuntu

# Installer/guest serial console on the host (exit with Ctrl+])
sudo virsh console agent-xubuntu

# Run GUI tests inside the guest
xvfb-run -a ./your-test-command

# Inspect provisioning inside the guest
sudo cloud-init status --long
sudo journalctl -u cloud-final
```

The random local GUI password is stored at `<state_dir>/<vm_name>.password`;
SSH uses `<state_dir>/id_ed25519`.

## Development

The project follows the initialized template structure:

```text
.github/workflows/       tests.yaml and release-triggered publish.yaml
agent_worker/         SubcommandApplication, VM modules, py.typed and _version.py
  guest/                standalone guest helpers
  resources/            application schema, fallback configs and guest resources
docs/                   Sphinx source, Makefile and Windows make helper
examples/               executable Python usage example
resources/              external development resources
scripts/                PyInstaller entry point
tests/                  pytest suite
agent_worker_config.json
logging.yaml
setup.py, setup.cfg, MANIFEST.in
pyproject.toml, tools/_build_backend.py
requirements.txt, tox.ini, pytest.ini
versioneer.py, .gitattributes
pyinstaller.spec, LICENSE
```

Install `requirements.txt` and the editable project as shown above, then run:

```bash
tox                                  # includes Python 3.11 and 3.12 test environments
tox -e type,lint,cli,pytest,docs,pyinstaller  # use the current interpreter
tox -e lint_update                   # Black formatting, template line length 160
python -m build --no-isolation
pipx install --force ./dist/agent_worker-latest.tar.gz # Install the package for this user
twine check dist/agent_worker-[0-9]*.tar.gz dist/*.whl
make -C docs html
python examples/example.py
dist/main --help                     # after tox -e pyinstaller
```

Each source build automatically updates `dist/agent_worker-latest.tar.gz` to
point to the newly built archive. The link is relative and is also created in the
selected output directory when using `--outdir`. Wheel-only builds leave it alone.
`pyproject.toml` selects a small build hook in `tools/_build_backend.py`; setuptools and
Versioneer continue to handle packaging. Package checks and uploads use the
versioned archives, excluding the local `latest` alias.

Versioneer computes the version from Git tags/revisions for source checkouts,
sdists, wheels, archives and frozen executables. Release by tagging a version
and publishing a GitHub release; the template's publish workflow waits for CI
and uses the repository's `PYPI_API_TOKEN` secret. No release is made by these
local changes. CI runs Linux/Python 3.11 and 3.12, with real tmux integration tests.

Tests use fake host commands for lifecycle, SSH and share failure recovery, verify
cloud-init resources and ISO checksums, and exercise the workspace with real tmux
when available. They do not create, destroy or alter a real VM.

See [development and template alignment](docs/src/development.rst) for the exact
mapping to the template, framework lifecycle and tooling. Initializer-only files
are omitted as the template directs; VM examples and docs replace demo content.

The package separates `main.py` (actions/configuration), `settings.py`, `system.py`
(commands, locks and sudo), `vm.py` (lifecycle/SSH), `shares.py`, `provision.py`,
standalone `guest/` helpers and packaged `resources/`. The GitHub machine-account
issue/branch/PR workflow is a future extension; this migration covers the existing
VM functionality.
