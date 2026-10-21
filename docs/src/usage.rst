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
variables, which override JSON values and built-in defaults.

.. code-block:: bash

   agent_worker --config agent_worker_config.json vm create --vcpus 4
   agent_worker --data-dir /srv/agent-worker vm status

Global options precede the action. Existing ``VM_NAME``, ``DATA_DIR``,
``STATE_DIR``, ``IMAGE_DIR``, ``AGENT_USER``, ``AGENT_EXPORT_DIR`` and other
legacy environment settings remain supported, including the ``CODEX_*`` aliases.
See the repository README for the full settings table.

A checkout or editable installation retains ``<checkout>/.data``. A normal
installation uses ``${XDG_DATA_HOME:-~/.local/share}/agent-worker``. To manage
an existing VM after installing a wheel, set ``DATA_DIR`` to its existing data
path. No host authentication keys or home directory are shared automatically.

The root ``logging.yaml`` follows the template's console/file logging layout;
console output goes to stderr so machine-readable quota output stays valid JSON.
The package includes a fallback logging configuration for installed use.
