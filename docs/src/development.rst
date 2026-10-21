Development and architecture
============================

Template alignment
------------------

This project is an initialized instance of
`twyleg/template_project_python <https://github.com/twyleg/template_project_python>`_,
using template commit ``1a7855da288716919a68dbdc1e3c5d603368e9ca`` as the reference.
The application name is ``agent_worker`` and the distribution is
``agent-worker`` (Python packaging normalizes underscores to hyphens).

.. list-table:: Template structure
   :header-rows: 1
   :widths: 32 68

   * - Files
     - Responsibility
   * - ``setup.py``, ``setup.cfg``, ``MANIFEST.in``
     - Setuptools metadata, package resources, Versioneer commands and source distribution
   * - ``pyproject.toml``, ``tools/_build_backend.py``
     - Setuptools build hooks and automatic latest-source-archive symlink
   * - ``versioneer.py``, ``_version.py``, ``.gitattributes``
     - Git/tag-derived versions in source, wheels, sdists and Git archives
   * - ``requirements.txt``, ``tox.ini``, ``pytest.ini``
     - Template dependency pins, pytest, Black, mypy and all tooling environments
   * - ``agent_worker/main.py``, ``__main__.py``, ``py.typed``
     - SubcommandApplication entry point, module execution and package typing marker
   * - Root and packaged JSON configuration and ``logging.yaml``
     - Framework schema validation, settings and console/file logging
   * - ``docs/``, ``examples/``, ``resources/``
     - Sphinx documentation, executable project examples and external development resources
   * - ``pyinstaller.spec``, ``scripts/pyinstaller_entry_point.py``
     - Standalone executable with application and framework resources
   * - ``.github/workflows/tests.yaml`` and ``publish.yaml``
     - CI checks and release-triggered publication to PyPI
   * - ``LICENSE``
     - The template's GPL-3.0 license

The initializer's own ``template_config.yaml`` and
``test_template_initializer.yaml`` workflow are omitted, as directed by the
template's initialization configuration. The initializer utility is also excluded
from development dependencies: its framework/type-stub pins conflict with the
current application framework. Use it in a separate environment when initializing
a new project. Placeholder tests, images, draw.io diagrams and example data are
replaced by VM documentation, tests and examples.

CI targets Linux with Python 3.11 and 3.12, reflecting the host's KVM/libvirt and
``fcntl`` requirements and the use of ``tomllib``. The template's application tooling remains;
The project uses the template's setup/Black/tox conventions. A minimal
``pyproject.toml`` selects a local backend that delegates to setuptools and
updates the latest source archive link after a successful source build.
The backend also generates Bash, Zsh and Fish integration scripts into the ignored
``completions/`` directory before building a source distribution, wheel or editable
wheel. ``setup.py`` installs them as data files under the standard ``share/``
directories. Generation imports only argcomplete, which is declared as a build
dependency; it does not import the application or its runtime configuration.

Application lifecycle
---------------------

``Application`` subclasses ``SubcommandApplication`` from
``simple-python-app==0.4.1``. Private setup methods register nested command paths
with ``add_subcommand`` and add options through each returned command's
``parser``. The framework dispatches directly to callbacks registered alongside each command.
SSH argument preparation uses a local handler. ``run`` prepares the VM, normalizes
successful completion to exit code zero and handles operational errors around dispatch.
The framework also owns argument setup, logging initialization, configuration
search/schema validation and the initialization stages. Startup uses the inherited
``start`` method directly. Its optional interactive command shell is disabled to
preserve the command-line workflow.

``settings.py`` resolves JSON/environment/CLI precedence. ``system.py`` owns
external commands, locking and scoped sudo authorization. ``vm.py`` provides
libvirt lifecycle and SSH, ``shares.py`` manages recoverable virtiofs records,
and ``provision.py`` generates autoinstall data and provisions the guest.
Frozen builds restore the host's shared-library search path for external commands,
as described in `PyInstaller's subprocess guidance
<https://pyinstaller.org/en/v6.10.0/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application>`_.
Standalone helpers in ``guest/`` require only the guest's standard Python
installation. Packaged resources remain available in wheels and frozen binaries.

Tooling
-------

.. code-block:: bash

   python3.12 -m venv venv
   source venv/bin/activate
   python -m pip install -r requirements.txt
   python -m pip install -e .
   tox

Tox provides the template's ``type``, ``lint``, ``lint_update``, ``cli``,
``pytest``, ``docs`` and ``pyinstaller`` environments, plus ``py311`` and
``py312`` test environments. The default ``tox`` invocation requires both Python
interpreters. For one locally available interpreter:

.. code-block:: bash

   tox -e type,lint,cli,pytest,docs,pyinstaller
   tox -e lint_update

Builds use ``--no-isolation`` after installing ``requirements.txt`` so the
template's pinned setuptools/wheel and Twine versions are used together. The CLI
tox environment checks its own output directory instead of stale artifacts.
Each source build creates or replaces ``agent_worker-latest.tar.gz`` in its
output directory with a relative symlink to the archive returned by setuptools.
The default location is ``dist/agent_worker-latest.tar.gz``; ``--outdir`` is
also supported. Versioneer is declared in ``build-system.requires`` so
``setup.py`` can import it during isolated pip/pipx installations. The CLI tox
environment also builds a wheel from the source archive in an isolated build
environment. Failed source builds preserve the previous link. The PyPI
workflow removes the alias before uploading the versioned distributions.

Black uses the template's 160-column line length. All maintained functions
annotate their parameters and return values; implicit ``self``/``cls`` parameters
and generated Versioneer files follow their standard conventions. Mypy checks
function bodies and rejects unannotated definitions across the package, tests
and examples. Unit tests mock host management commands, with real tmux
integration tests when tmux is available; they do not alter a real VM.

.. code-block:: bash

   python -m build --no-isolation
   twine check dist/agent_worker-[0-9]*.tar.gz dist/*.whl
   make -C docs html
   tox -e pyinstaller
   dist/main --help

Versioneer derives the version from Git tags and revisions. Create a version tag
for a release, rather than editing a version string. The PyPI workflow runs on
published GitHub releases, waits for tests/tooling, builds distributions, and
uses the repository's ``PYPI_API_TOKEN`` secret. GitHub checkout fetches tag
history. Adding this workflow does not itself publish or release anything.
