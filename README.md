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

## Local workflow

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

## GitHub workflow

The GitHub workflow adds a second, opt-in workflow alongside the local workflow.
You are the project manager and use your personal GitHub account. The developer
uses a separate machine account authenticated **inside the VM**. Issues and PRs
hold the conversation and development history; you review and merge the work.

The Python worker uses PyGithub and polls registered repositories every 60 seconds,
queues open issues assigned to the developer, and runs one unattended `codex exec`
turn at a time. Polling and queue management do not use AI. Manager replies on an
issue, PR comments, inline review comments and submitted “Request changes” reviews
queue another turn. Own replies and comments from other accounts do not trigger
work. Approval alone does not trigger a turn.

### Set up the accounts and repository

Create the machine account on GitHub and authenticate Codex inside the VM using
the existing local workflow. GitHub API operations use **PyGithub**, installed as
an application dependency on the host and in a dedicated virtual environment in
the guest. GitHub CLI is not required. Git itself still handles clone, commit and
push operations.

Create two personal access tokens, each under its own account:

- **Host / project manager:** a fine-grained token limited to the selected
  repositories, with Administration **read** for protection checks, Contents
  **read** for CODEOWNERS and branches, and Metadata read. Inviting collaborators
  with `--invite` additionally needs Administration **write** on the host token;
  you can instead invite through GitHub's UI and keep the host token read-only.
- **Guest / machine account:** for the personal-repository collaborator workflow,
  use a **classic token** with `repo` scope (or `public_repo` if all repositories are
  public). Add `workflow` if tasks need to push changes to GitHub Actions files.
  Fine-grained tokens currently cannot contribute as a repository/outside
  collaborator. The token cannot grant rights beyond the machine account's
  actual repository permissions; keep that account as a write collaborator.

See [GitHub's token documentation and collaborator limitations](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

```bash
# If upgrading an existing installation, first stop the worker:
agent_worker github stop

# This hidden token prompt runs locally; the manager token stays on the host:
agent_worker github login --host

# Installs/updates the guest Python environment; preserves registered repositories:
agent_worker github configure --developer YOUR_MACHINE_ACCOUNT --manager YOUR_USERNAME

# This hidden token prompt runs inside the VM as the machine account:
agent_worker github login
```

On a fresh setup, omit `github stop`. Both login commands validate the token with
GitHub before saving it; the guest also checks that it belongs to the configured
developer. Host add/check commands enforce the configured manager identity.
`github configure` installs a systemd user service and enables guest user lingering,
but leaves polling disabled on a fresh setup. The guest environment lives at
`~/.local/lib/agent-worker/github-venv`; host dependencies never need to be copied
from the host Python installation into the VM.

Tokens are stored separately on each machine at
`${XDG_CONFIG_HOME:-~/.config}/agent-worker/github-token`, with mode `0600`. They are
not part of the shared configuration JSON or job logs. Replace/renew a token by
running the corresponding login command again. For automation, use
`github login [--host] --token-stdin` to read a token from stdin without putting it
in command arguments, or provide `AGENT_WORKER_GITHUB_TOKEN` in the relevant
host/guest process environment. Environment tokens take precedence over saved
tokens and are not automatically forwarded over SSH or installed into the guest
service environment.

The guest uses an ephemeral Git credential helper for github.com HTTPS operations.
It reads the same developer token used by PyGithub, overrides old helpers for each
Git process, and never embeds the token in repository URLs or persistent Git
configuration. Existing `gh` credentials are not imported.

Add the developer as a write collaborator in GitHub's Settings → Collaborators,
or use `--invite` below. The developer must have write access, not administrator
access. In each repository, put this as the **last noncomment rule** in the
CODEOWNERS file on the default branch and any existing `main`/`master` branches:

```text
* @YOUR_USERNAME
```

GitHub checks `.github/CODEOWNERS`, then `CODEOWNERS`, then `docs/CODEOWNERS`, using
only the first file found. Keeping the manager as the sole owner in the final
catch-all rule makes approval by that specific person mandatory for every file.
The manager needs write permission. Commit this setup yourself before onboarding.

For the default branch and any existing `main`/`master`, configure **classic branch
protection** in GitHub's Settings → Branches:

- Require a pull request before merging, with at least one approval.
- Require review from Code Owners.
- Dismiss stale approvals when new commits are pushed.
- Administrator bypass is allowed; “Do not allow bypassing” may remain unchecked.
- Disable force pushes, branch deletion and PR bypass allowances.

The application checks these settings; it does not replace existing protection,
create CODEOWNERS, or change repository rules. Ruleset-only repositories are not
yet supported by this verifier. GitHub Pro supports this for public and private
repositories **owned by your personal account**; organization-owned private
repositories need an appropriate organization plan.
[GitHub branch protection documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
and [code owner documentation](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners).

```bash
# After inviting the developer in the GitHub UI:
agent_worker github add OWNER/REPO

# Or send the invitation with your host token, then register:
agent_worker github add OWNER/REPO --invite --developer YOUR_MACHINE_ACCOUNT

agent_worker github check
agent_worker github start
```

Adding the machine account as a collaborator in GitHub's web UI grants access but
**does not register the repository with the worker**. Run `github add OWNER/REPO`
for each project, including projects invited through the UI. The worker polls only
that explicit list; it does not automatically scan every accessible repository.
Each poll logs the registered repository names, and `github status` includes the
running worker's repository list. Stop the worker before adding a project, then
start it again to load the updated list.

`github add` first uses the host's project-manager token to check all review
settings, bypass restrictions and CODEOWNERS. Only after that succeeds does the
guest accept any pending invitation for the selected repository, verify developer
access and confirm that its default branch and existing `main`/`master` branches
report as protected. `github check` repeats the full policy verification from the
host for every registered repository. The VM must be reachable so the host can
read the configured identities and repository list. No credentials are returned
by this read or forwarded from the host to the guest.

A missing or unreadable policy blocks registration. The guest uses only the machine
account's token for polling, comments, Git operations and PRs. It does not inspect
the administrative protection endpoint or store an owner policy token. The former
`github protection-login` command has been removed. Updated guest helpers delete
the old `~/.config/agent-worker/github-protection-token` file when invoked; an
existing token can also be revoked on GitHub.

When upgrading, stop the worker before configuring the new environment and tokens,
then start it again after `github add` or `github check` succeeds. The repository
list, queue, checkouts and completed task results are preserved.

### Work on issues and review PRs

Create an issue and assign it to the machine account. The worker clones into a
separate guest directory, `~/workspace-github/OWNER/REPO/issue-N`, and works on
`agent-worker/issue-N`. These checkouts are independent of host-mounted projects.
If clarification is needed, it posts a question on the issue and waits for your
reply. Otherwise it commits and pushes the feature branch, opens a PR linked with
`Closes #N`, requests you as reviewer, and links the PR from the issue.

Reply on the issue or PR, add inline review comments, or submit a review requesting
changes to queue follow-up work on the same branch and PR. A published inline
comment alone is sufficient; a “Request changes” review is not required. Pending
review comments are private to their author: finish with **Submit review** (using
Comment or Request changes) before expecting the machine account to see them.
Poll logs report PR discussion, inline comment and submitted-review counts,
including the number of inline comments from the configured manager. After approval, merge the PR yourself.
The worker never merges PRs or directly pushes to the default branch.

### Request a code review

For a repository registered with `github add`, create a PR from your own branch
and request the machine account as a reviewer in GitHub's **Reviewers** sidebar.
No assigned issue is required for a review request. The worker polls open,
non-draft PRs for direct requests to that account and adds a **review** task to
the same persistent queue used by implementation tasks. It skips PRs authored by
the machine account itself. The dashboard labels queued/active review work as PR
reviews; completed review jobs have state `reviewed` in `github status`.

Review tasks use a separate `~/workspace-github/OWNER/REPO/review-pr-N` checkout,
fetch GitHub's PR head ref (including fork PRs), and run Codex with a read-only
sandbox against the recorded base/head commits. They publish a **Comment** review
with a summary and inline findings; they do not push fixes, approve or merge the
PR. Findings that cannot be anchored to an actual changed line are retained in
the review summary. The review records the exact commit SHA it inspected.

The worker rechecks that the PR is open, the account is still requested, and the
request event and base/head commits are unchanged before publication. Closing the
PR or withdrawing the request cancels pending review work. Updated commits or a
new request supersede the old queued result; changes after the final API check
can still make a review outdated, but it remains pinned to its inspected commit.
After a review is complete, use GitHub's **Re-request review** control for another
pass. Request-event IDs distinguish re-requests even when the commit is unchanged,
and a request left visible after a Comment review does not cause repeated reviews.
Movement of the base branch alone also does not repeat a completed review.

Existing reviews/comments are supplied as context. Completed Codex review output
is retained under `runs/JOB_ID/review-result.json`; retries reuse it. Published
reviews carry a marker so a retry after a lost response does not post duplicates.
The existing guest machine-account token handles reviews with its `repo` scope;
no manager token is copied to the VM. Deployment uses the normal helper refresh
and `github restart-service`; the queue schema updates in place without removing
existing jobs.

### Runtime checks and status

Implementation tasks recheck identity, assignment and the observable protected flag for
its default branch and existing `main`/`master` branches before starting a task
and again before publication. It blocks work if a branch is unprotected or its
status is unavailable. This lightweight check cannot detect changes to individual
approval settings while a branch still reports as protected. Run the host's
`agent_worker github check` to revalidate those settings and CODEOWNERS. Closing an issue, removing the assignment, or
closing its PR stops subsequent publication. New manager feedback arriving during
a run queues an updated turn before publication. GitHub changes and the final push
are separate API operations, so these checks cannot eliminate every concurrent
change between the final check and publication; GitHub enforces branch protection.

The workspace's top-right dashboard shows GitHub activity, queued issues, questions
waiting for your answer, PRs awaiting review and failures alongside system and
Codex usage. The queue summary stays near the top in compact panes; use Up/Down
or j/k in the dashboard for longer lists. Reconnect with `vm connect` to upload
updated helpers. Reconnecting automatically reloads a recognized dashboard pane
when its installed code has changed, including dashboards created before version
tracking existed. Codex and shell panes, pane IDs, layout and working directories
are preserved; an unchanged dashboard is not restarted.

The footer's **[p] Poll now** control requests an extra GitHub poll. Click the
top-right pane to focus it and press `p` (or `P`). An idle worker notices the local
request within approximately one second. This leaves the configured automatic
interval unchanged, starts no extra worker and coalesces repeated presses into
one pending poll. During an active task, the poll waits until that task finishes;
it never interrupts Codex. GitHub rate-limit cooldowns also take precedence.

The dashboard confirms the request and shows **Manual poll queued** while it is
pending. If the service is stopped or unavailable, it asks you to start it; if an
older worker is still running, it asks for a restart to enable the control. The
worker logs when it accepts a manual request. After updating, use `vm connect`
to reload the dashboard and restart the worker between tasks to enable manual
polling.


```bash
agent_worker github status              # JSON: activity, job IDs, queue and errors
agent_worker github stop                # stop current work and disable boot startup
agent_worker github restart-service     # restart the existing guest service
agent_worker github log                 # follow its journal; Ctrl-C stops following
agent_worker github run --once          # poll once and execute at most one job
agent_worker github retry JOB_ID        # requeue a failed job while stopped
agent_worker github start

# Unregister a repository (does not revoke its GitHub collaborator access):
agent_worker github stop
agent_worker github remove OWNER/REPO
agent_worker github start
```

Stop the service before changing configuration, registering/removing repositories,
retrying jobs, or running the worker in the foreground. `github start` enables it
at subsequent VM boots; `github stop` disables that startup. Shutdown of the VM
also stops work. Interrupted tasks are requeued at startup, with checkouts and
completed Codex results retained. Failed tasks stay failed until explicitly retried
or new manager feedback queues an updated task; publication retries reuse the
completed result and existing PR. Replies have persistent markers to prevent
repeat comments after a lost response. Git pushes are fast-forward only; divergent
branches need manual resolution. A Codex turn has a two-hour timeout.

Guest state lives under `~/.local/state/agent-worker/github/`: `queue.sqlite3`,
`status.json`, and `runs/JOB_ID/` containing the prompt, result and Codex log.
Configuration is `~/.config/agent-worker/github.json`.

The worker logs startup/shutdown, every poll and its duration, assigned issue counts
per repository, new/updated versus unchanged work, queue totals, blocked repository
checks, task transitions and failures. Task logs identify cloning/synchronization,
Codex start/end (and the detailed output path), result reuse, pre-publication
checks, questions awaiting a reply, commits, pushes, PR creation/update and reviewer
requests. A long-running task emits an activity message once a minute. Polling is
serial with task execution: the next poll happens after the active task finishes
and the configured interval elapses.

There is no lifetime limit on the number of polls. GitHub normally allows 5,000
authenticated REST requests per hour per user, shared across that user's tokens
and other API clients, plus secondary burst/content limits. A poll makes multiple
API requests, and pagination or more issues/PRs increases that count. The default
60-second interval suits small installations; it is not a guarantee for arbitrary
repository counts. GitHub Pro does not increase the normal PAT request budget.

The worker logs the remaining request budget reported by GitHub and includes
`rate_limit` and `rate_limited_until` in `github status`. When GitHub rate-limits a
request, it stops the poll and delays subsequent API access according to
`Retry-After` and the reset timestamp, with a minimum one-minute exponential
backoff for successive secondary limits. A job interrupted by rate limiting stays
queued, retaining its checkout and completed Codex result. Other permission errors
remain errors rather than being treated as rate limits. Quota/backoff is tracked
in the running process; restarting the service resets that local tracking.
[GitHub rate-limit documentation](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

Timestamped INFO/ERROR messages go immediately to stderr (captured by the systemd
journal) and to `~/.local/state/agent-worker/github/workflow.log`. This operational
log rotates at 5 MiB, keeping three backups. Log messages use IDs, counts and
stages; issue/PR text and full Codex output are kept in the separate job artifacts.

```bash
# Recent service events, or follow them live:
agent_worker vm ssh 'journalctl --user -u agent-worker-github.service -n 100 --no-pager'
agent_worker github log

# The same operational log is available for foreground runs:
agent_worker vm ssh 'tail -f ~/.local/state/agent-worker/github/workflow.log'

# Detailed model output for a job shown by github status:
agent_worker vm ssh 'tail -f ~/.local/state/agent-worker/github/runs/JOB_ID/codex.log'
```

To load logging changes into an already-running worker, upload the updated helpers
with `vm connect` (or `github check`), then restart the service. Restarting interrupts
an active task; its retained job is requeued at startup. Prefer restarting between
tasks:

```bash
agent_worker github restart-service
```

Repository allowlisting, manager-only follow-up triggers and GitHub protection
provide the workflow boundaries. Codex retains the VM's existing full-access mode;
this is not an isolation boundary between jobs or from other VM credentials.
Model selection through labels and GitHub webhooks are left for a later version.

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
4. VM identity saved in the selected data directory (`vm.json`).
5. Defaults, including local `.data` discovery.

Global options go **before** the action:

```bash
agent_worker --data-dir /srv/agent-worker --vm-name my-agent vm status
agent_worker --config agent_worker_config.json vm create --ram-mb 8192 --vcpus 4 --disk-gb 50
```

VM, project and Codex commands print the selected VM, resolved data directory and
libvirt URI before doing any work, for example:

```text
VM: agent-xubuntu | Data: /home/you/.local/share/agent-worker | URI: qemu:///system
```

This information goes to stderr so JSON and remote command output on stdout
remain usable. Help, shell completion and guest-local setup do not print it.

The framework loads `agent_worker_config.json` or `.agent_worker_config.json`
from the current directory, then your home directory. The root configuration is
the editable template-style starting point; a packaged empty configuration supplies
defaults when no user file exists. An explicit missing file or a configuration that
does not match the packaged JSON schema is an error. Relative paths resolve against
the current directory. Unknown keys are rejected.

| JSON setting | Environment variable | Default |
| --- | --- | --- |
| `vm_name` | `VM_NAME` | Saved/adopted VM name, otherwise directory-specific; see below |
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

Without an explicit `data_dir` setting, the CLI uses `.data` in the **current
working directory** if it is a directory or a symlink to one. Otherwise it uses
`${XDG_DATA_HOME:-~/.local/share}/agent-worker`. It does not search parent
directories or use the package/checkout location. A `.data` file or broken
symlink produces an error instead of silently selecting the user-wide VM.
This behavior is the same for source checkouts, editable installs and wheels.

The selected data directory determines the VM. New local or explicitly selected
directories get a unique default domain name (`agent-xubuntu-<directory-hash>`);
the user-wide directory keeps `agent-xubuntu` by default. Creation saves the
domain name, libvirt URI and guest login user in `<data_dir>/vm.json`. Later
commands read that identity automatically, including after the directory moves
(libvirt's storage paths still need updating as described below).

Existing directories without `vm.json` adopt the name of their single
`images/*.qcow2` disk, including custom `IMAGE_DIR` locations. Multiple disks
require an explicit VM name. Settings lookup and shell completion do not create
directories or metadata. Creation writes `vm.json` before installing the VM, so
an interrupted installation retains its selected identity.

For separate project VMs, create an empty `.data` directory in each project:

```bash
cd ~/dev/project-one
mkdir .data
agent_worker vm create
agent_worker vm connect

cd ~/dev/project-two
mkdir .data
agent_worker vm create
agent_worker vm connect
```

Each directory selects its own VM, disk, SSH key and share records. To select an
existing VM locally, symlink `.data` to its data directory instead. Two symlinks
to the same data directory select the same VM.

Explicit settings remain available:

```bash
agent_worker --data-dir /srv/my-agent vm status
agent_worker --data-dir /srv/my-agent --vm-name custom-agent vm create
```

The second command saves `custom-agent` in that data directory; subsequent
commands only need to select the directory. Explicit `vm_name` values in JSON,
`VM_NAME`, or `--vm-name` still override the saved/generated name. Remove a fixed
`vm_name` from shared configuration to allow automatic per-directory selection.
Explicit `STATE_DIR`, `IMAGE_DIR` and export overrides remain independent; avoid
pointing multiple instances at shared state. To select a directory in a shell:

```bash
export DATA_DIR=/srv/my-agent
agent_worker vm status
```

Share records retain their existing format. New runtime data has this layout:

```text
<data_dir>/
  vm.json
  images/
    <vm-name>.qcow2
    iso/
  state/
    id_ed25519
    id_ed25519.pub
    known_hosts
    <vm-name>.password
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
standalone `guest/` helpers and packaged `resources/`. `github.py` manages the
opt-in GitHub workflow through the standalone `guest/github_workflow.py` worker.
