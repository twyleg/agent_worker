Usage
=====

Installation
------------

Python 3.11 or newer is required. Install the package on the host as your normal
user; creation obtains sudo authorization for individual privileged operations.

.. code-block:: bash

   python3 -m pip install --user .
   # Alternatively, use pipx to respect externally managed Python installations.
   pipx install .

Ensure the user script directory is on ``PATH``. The ``agent_worker`` console
command and ``python -m agent_worker`` use the same application.

Shell completion
----------------

The CLI can generate completion scripts for Bash, Zsh and Fish. Completion covers
subcommands, options, fixed choices, directory paths and attached project names.
Project completion reads local records using the selected configuration and data
directory; it does not connect to the VM or initialize application logging.

Builds ship scripts for all three shells. pipx 1.16.0 and newer expose them under
``~/.local/share``; a default ``pip install --user`` installs them there directly.
Fish automatically discovers
``~/.local/share/fish/vendor_completions.d/agent_worker.fish``. Bash requires its
``bash-completion`` framework, and Zsh requires
``~/.local/share/zsh/site-functions`` on ``fpath`` before ``compinit``.

Older pipx versions and pip installs inside virtual environments need manual
shell registration. For Bash, add this to ``~/.bashrc``:

.. code-block:: bash

   eval "$(agent_worker completion bash)"

For Zsh, add this after ``compinit`` in ``~/.zshrc``:

.. code-block:: zsh

   eval "$(agent_worker completion zsh)"

For Fish, load completion in the current session:

.. code-block:: fish

   agent_worker completion fish | source

To persist manual Fish setup:

.. code-block:: fish

   mkdir -p ~/.config/fish/completions
   agent_worker completion fish > ~/.config/fish/completions/agent_worker.fish

Type a space after ``agent_worker`` before pressing Tab. In Fish,
``complete -C 'agent_worker '`` should list the application's subcommands.
Remote command arguments passed to ``vm ssh`` are not completed against the guest.

VM and projects
---------------

.. code-block:: bash

   agent_worker vm create
   agent_worker vm start
   agent_worker project attach ~/dev/my-project
   agent_worker vm connect
   agent_worker codex usage --json
   agent_worker vm shutdown

Creation installs and boots the guest unattended. Authenticate Codex inside the
guest once. The tmux workspace preserves running Codex and shell panes between
connections. A single shared project becomes a new workspace's starting
directory; multiple projects live under ``~/workspace/<project-name>``.

.. code-block:: bash

   agent_worker vm status
   agent_worker vm ssh 'cd ~/workspace/my-project && git status'
   agent_worker project attach ~/other/my-project other-project
   agent_worker project status
   agent_worker project detach other-project
   agent_worker project detach --all

Repeat attach after a guest or host restart to restore live devices. Failed
mount/detach operations preserve tracking so that they can be retried.
Destruction and recreation retain the cached ISO and dedicated SSH key:

.. code-block:: bash

   agent_worker vm destroy
   agent_worker vm create --recreate

Configuration
-------------

``GenericApplication`` loads and validates ``agent_worker_config.json`` or
``.agent_worker_config.json`` from the current directory, then the user's home
directory. A packaged empty configuration supplies defaults when neither exists.
Use ``--config PATH`` for an explicit file. CLI options override environment
variables, which override JSON values, the selected data directory's saved VM
identity, and built-in defaults.

.. code-block:: bash

   agent_worker --config agent_worker_config.json vm create --vcpus 4
   agent_worker --data-dir /srv/agent-worker vm status

Global options precede the action. Existing ``VM_NAME``, ``DATA_DIR``,
``STATE_DIR``, ``IMAGE_DIR``, ``AGENT_USER``, ``AGENT_EXPORT_DIR`` and other
legacy environment settings remain supported, including the ``CODEX_*`` aliases.
See the repository README for the full settings table.

Without an explicit data directory, all installations use ``.data`` in the
current working directory if it exists as a directory (including a symlink to
one). Otherwise they use ``${XDG_DATA_HOME:-~/.local/share}/agent-worker``. Parent
directories are not searched. A file or broken symlink at ``.data`` is an error.

Each new local data directory gets its own default VM name. Creation saves the
name, libvirt URI and guest user in ``vm.json`` within that directory. Future
commands select that VM automatically. Existing data without ``vm.json`` adopts
the name of its single qcow2 disk; multiple disks require an explicit VM name.
The user-wide directory retains the default name ``agent-xubuntu``.

.. code-block:: bash

   cd ~/dev/project-one
   mkdir .data
   agent_worker vm create
   agent_worker vm connect

Repeat in another project directory for an independent VM, or symlink ``.data``
to an existing data directory to select that VM. Explicit ``--data-dir``,
``--vm-name``, environment variables and JSON settings still override discovery.
Remove a fixed ``vm_name`` from shared JSON configuration to allow automatic
selection. No host authentication keys or home directory are shared
automatically.

The root ``logging.yaml`` follows the template's console/file logging layout;
console output goes to stderr so machine-readable quota output stays valid JSON.
The package includes a fallback logging configuration for installed use.
