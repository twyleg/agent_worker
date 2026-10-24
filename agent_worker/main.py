# PYTHON_ARGCOMPLETE_OK
"""Command registration and handlers using simple-python-app's SubcommandApplication."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import argcomplete
from argcomplete.completers import DirectoriesCompleter
from simple_python_app.subcommand_application import Command, SubcommandApplication

from agent_worker import __version__
from agent_worker.completion import project_selectors
from agent_worker.github import GitHubWorkflow
from agent_worker.provision import Provisioner
from agent_worker.settings import RESOURCE_DIR, Settings
from agent_worker.shares import Shares
from agent_worker.system import CommandError, host_environment
from agent_worker.vm import VM


class Application(SubcommandApplication):
    def __init__(self) -> None:
        log_dir = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "agent-worker/log"
        super().__init__(
            application_name="agent_worker",
            version=__version__,
            shell_enabled=False,
            application_config_schema_filepath=RESOURCE_DIR / "application_config_schema.json",
            application_config_search_paths=[Path.cwd(), Path.home(), RESOURCE_DIR],
            logging_default_config_filepath=RESOURCE_DIR / "logging.yaml",
            logging_logfile_output_dir=log_dir,
        )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.description = "Manage an isolated Xubuntu playground for coding agents."
        data_dir = parser.add_argument("--data-dir", type=Path, help="VM data directory (overrides DATA_DIR and local .data discovery)")
        setattr(data_dir, "completer", DirectoriesCompleter())
        parser.add_argument("--vm-name", help="Libvirt domain name (overrides VM_NAME and the selected data directory's VM)")
        parser.add_argument("--libvirt-uri", help="Libvirt connection URI")
        resource_dir = parser.add_argument("--resource-dir", type=Path, help="Directory containing custom provisioning resources")
        setattr(resource_dir, "completer", DirectoriesCompleter())

        self._setup_vm_commands()
        self._setup_project_commands()
        self._setup_codex_commands()
        self._setup_guest_commands()
        self._setup_github_commands()
        self._setup_completion_command()
        self._setup_upgrade_command()
        argcomplete.autocomplete(parser)

    # The framework annotates handlers as returning int; run() also accepts void callbacks.
    def _setup_vm_commands(self) -> None:
        self.root_command.add_subcommand("vm", help="Create, manage and connect to the VM")
        create = self.add_subcommand(
            command="vm create",
            help="Install and provision an unattended Xubuntu VM",
            description="Install Xubuntu and provision the coding agent's development environment.",
            handler=cast(Command.Handler, lambda args: Provisioner(self.vm).create(args.recreate)),
        )
        create.parser.add_argument("--recreate", action="store_true", help="Destroy and replace the existing VM and disk")
        for name in ("ram-mb", "vcpus", "disk-gb"):
            create.parser.add_argument(f"--{name}", type=int)
        self.add_subcommand(
            command="vm start",
            help="Boot an existing VM",
            description="Boot the VM, succeeding if it is already running.",
            handler=cast(Command.Handler, lambda args: self.vm.start()),
        )
        self.add_subcommand(
            command="vm shutdown",
            help="Request graceful shutdown",
            description="Request a graceful shutdown without deleting the VM.",
            handler=cast(Command.Handler, lambda args: self.vm.shutdown()),
        )
        self.add_subcommand(
            command="vm status",
            help="Show VM details, IP and shared projects",
            description="Inspect the VM through read-only libvirt access.",
            handler=cast(Command.Handler, lambda args: self.vm.status()),
        )
        destroy = self.add_subcommand(
            command="vm destroy",
            help="Delete the VM and disk, retaining ISO and SSH key",
            description="Remove the VM, disk and project exports while retaining reusable cached files.",
            handler=cast(Command.Handler, lambda args: self.vm.destroy(args.yes)),
        )
        destroy.parser.add_argument("-y", "--yes", action="store_true", help="Skip the deletion confirmation")
        self.add_subcommand(
            command="vm connect",
            help="Attach to the persistent Codex/tmux workspace",
            description="Upload workspace helpers and attach to the persistent tmux session.",
            handler=cast(Command.Handler, lambda args: self.vm.connect()),
        )

        def handle_ssh(args: argparse.Namespace) -> int:
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            self.vm.ssh(*command, tty=args.tty)
            return 0

        ssh = self.add_subcommand(
            command="vm ssh",
            help="Open a plain SSH shell or execute a remote command",
            description="Connect using the VM's dedicated SSH key.",
            handler=handle_ssh,
        )
        ssh.parser.add_argument("-t", "--tty", action="store_true", help="Request an SSH terminal")
        ssh.parser.add_argument("command", nargs=argparse.REMAINDER, help="Remote command and its arguments")

    def _setup_project_commands(self) -> None:
        self.root_command.add_subcommand("project", help="Attach or detach selected host projects")
        attach = self.add_subcommand(
            command="project attach",
            help="Hot-plug a host directory into the running VM",
            description="Share one selected host project with the guest using virtiofs.",
            handler=cast(Command.Handler, lambda args: Shares(self.vm).attach(args.path, args.name)),
        )
        project_path = attach.parser.add_argument("path", type=Path)
        setattr(project_path, "completer", DirectoriesCompleter())
        attach.parser.add_argument("name", nargs="?", help="Guest directory name (defaults to host directory name)")
        detach = self.add_subcommand(
            command="project detach",
            help="Unmount and remove a project share",
            description="Detach a project selected by guest name or host path, or detach all projects.",
            handler=cast(Command.Handler, lambda args: Shares(self.vm).detach(args.selector, args.all)),
        )
        selection = detach.parser.add_mutually_exclusive_group()

        def complete_projects(prefix: str, parsed_args: argparse.Namespace, **kwargs: object) -> list[str]:
            config_filepath = getattr(parsed_args, "config", None) or self.find_file(
                self.application_config_search_paths, self.application_config_search_filenames
            )
            return project_selectors(prefix, parsed_args, config_filepath)

        selector = selection.add_argument("selector", nargs="?", help="Guest name or host path")
        setattr(selector, "completer", complete_projects)
        selection.add_argument("--all", action="store_true", help="Detach every tracked project")
        status = self.add_subcommand(
            command="project status",
            help="List project shares",
            description="Show a compact table of projects and their host and guest mount status.",
            handler=cast(Command.Handler, lambda args: Shares(self.vm).status(args.verbose, args.color)),
        )
        status.parser.add_argument("-v", "--verbose", action="store_true", help="Show full paths and host mount details")
        status.parser.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="Highlight mount states (default: auto)")

    def _setup_codex_commands(self) -> None:
        self.root_command.add_subcommand("codex", help="Inspect the guest's Codex account")
        usage = self.add_subcommand(
            command="codex usage",
            help="Read live account rate limits without starting a turn",
            description="Query the Codex account authenticated inside the VM.",
            handler=cast(Command.Handler, lambda args: self.vm.usage(args.json)),
        )
        usage.parser.add_argument("--json", action="store_true")

    def _setup_github_commands(self) -> None:
        self.root_command.add_subcommand("github", help="Manage the GitHub workflow")
        configure = self.add_subcommand(
            command="github configure",
            help="Install the opt-in GitHub worker in the guest",
            description="Set the team identities, install the guest Python environment and user service; polling stays disabled.",
            handler=cast(Command.Handler, lambda args: GitHubWorkflow(self.vm).configure(args.developer, args.manager, args.interval)),
        )
        configure.parser.add_argument("--developer", required=True, help="Machine account GitHub username")
        configure.parser.add_argument("--manager", required=True, help="Your GitHub username and required reviewer")
        configure.parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (minimum 30)")
        login = self.add_subcommand(
            command="github login",
            help="Save a GitHub token for the guest or host",
            description="Validate and store a token using a hidden prompt; defaults to the guest machine account.",
            handler=cast(Command.Handler, lambda args: GitHubWorkflow(self.vm).login(args.host, args.token_stdin)),
        )
        login.parser.add_argument("--host", action="store_true", help="Store the project manager's token on the host")
        login.parser.add_argument("--token-stdin", action="store_true", help="Read the token from stdin instead of a hidden prompt")
        add = self.add_subcommand(
            command="github add",
            help="Verify protection and register a repository",
            description="Verify required manager approval using the host token, then accept the developer invitation and register in the guest.",
            handler=cast(Command.Handler, lambda args: GitHubWorkflow(self.vm).add(args.repository, args.invite, args.developer)),
        )
        add.parser.add_argument("repository", help="OWNER/REPO")
        add.parser.add_argument("--invite", action="store_true", help="Invite the machine account using the host token")
        add.parser.add_argument("--developer", help="Machine account username for --invite")
        for action, help_text in (
            ("check", "Verify full repository protection using the host token"),
            ("status", "Show workflow state and queue as JSON"),
            ("start", "Enable polling now and at guest boot"),
            ("stop", "Stop polling and disable automatic startup"),
            ("restart-service", "Restart the guest GitHub workflow service"),
            ("log", "Follow the guest GitHub workflow service journal"),
            ("run", "Run the worker in the foreground"),
            ("retry", "Retry a failed job"),
            ("remove", "Unregister a repository"),
        ):

            def handle(args: argparse.Namespace, action: str = action) -> None:
                GitHubWorkflow(self.vm).action(
                    action, once=getattr(args, "once", False), job=getattr(args, "job", None), repository=getattr(args, "repository", None)
                )

            subcommand = self.add_subcommand(command=f"github {action}", help=help_text, description=help_text, handler=cast(Command.Handler, handle))
            if action == "run":
                subcommand.parser.add_argument("--once", action="store_true", help="Poll once and run at most one job")
            elif action == "retry":
                subcommand.parser.add_argument("job", type=int)
            elif action == "remove":
                subcommand.parser.add_argument("repository", help="OWNER/REPO")

    def _setup_guest_commands(self) -> None:
        self.root_command.add_subcommand("guest", help="Repair guest tooling (run inside the guest)")
        setup = self.add_subcommand(
            command="guest setup",
            help="Install or update guest tooling",
            description="Run as root inside the guest to repair or update its development tools.",
            handler=cast(Command.Handler, lambda args: Provisioner(self.vm).setup_guest()),
        )
        setup.parser.set_defaults(show_vm_context=False)

    def _setup_completion_command(self) -> None:
        completion = self.add_subcommand(
            command="completion",
            help="Generate a shell completion script",
            description="Print a completion script for Bash, Zsh or Fish to load in your shell.",
            handler=cast(Command.Handler, lambda args: print(argcomplete.shellcode(["agent_worker"], shell=args.shell), end="")),
        )
        completion.parser.add_argument("shell", choices=("bash", "zsh", "fish"))
        completion.parser.set_defaults(show_vm_context=False)

    def _setup_upgrade_command(self) -> None:
        upgrade = self.add_subcommand(
            command="upgrade",
            help="Upgrade the host's pipx-installed agent-worker package",
            description="Hand off to pipx upgrade agent-worker --pip-args=--no-cache-dir.",
            handler=cast(Command.Handler, lambda args: self.upgrade()),
        )
        upgrade.parser.set_defaults(requires_vm=False, show_vm_context=False)

    @staticmethod
    def upgrade() -> None:
        environment = host_environment()
        pipx = shutil.which("pipx", path=environment.get("PATH"))
        if pipx is None:
            raise RuntimeError("pipx was not found on PATH. Install pipx and use it to install agent-worker before running upgrade.")
        sys.stdout.flush()
        sys.stderr.flush()
        # Replace this process before pipx replaces package files. Nothing in
        # agent_worker needs to import, execute or clean up after the upgrade.
        os.execve(pipx, [pipx, "upgrade", "agent-worker", "--pip-args=--no-cache-dir"], environment)

    def load_settings(self, args: argparse.Namespace) -> Settings:
        names = ("data_dir", "vm_name", "libvirt_uri", "resource_dir", "ram_mb", "vcpus", "disk_gb")
        return Settings.load(self.application_config, {name: getattr(args, name, None) for name in names})

    def run(self, args: argparse.Namespace) -> int:
        try:
            if getattr(args, "requires_vm", True):
                settings = self.load_settings(args)
                if getattr(args, "show_vm_context", True):
                    print(f"VM: {settings.vm_name} | Data: {settings.data_dir} | URI: {settings.libvirt_uri}", file=sys.stderr, flush=True)
                self.vm = VM(settings)
            super().run(args)
            return 0
        except KeyboardInterrupt:
            return 130
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return max(1, exc.returncode) if isinstance(exc, CommandError) else 1


def main(argv: list[str] | None = None) -> None:
    application = Application()
    raise SystemExit(application.start(argv))


if __name__ == "__main__":
    main()
