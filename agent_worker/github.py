"""Host commands for the opt-in guest GitHub workflow."""

from __future__ import annotations

import base64
import io
import json
import shlex
import tarfile
from typing import Any
from urllib.parse import quote

from agent_worker.guest.github_client import GitHub, RateLimitError, prompt_token, save_token
from agent_worker.guest.github_workflow import validate_identity, validate_repo
from agent_worker.vm import GUEST_DIR, VM


def protection_problems(protection: dict[str, Any], owners: str, manager: str) -> list[str]:
    problems = []
    reviews = protection.get("required_pull_request_reviews") or {}
    if reviews.get("required_approving_review_count", 0) < 1:
        problems.append("require at least one approving review")
    if not reviews.get("require_code_owner_reviews"):
        problems.append("require code owner reviews")
    if not reviews.get("dismiss_stale_reviews"):
        problems.append("dismiss stale approvals after new commits")
    if any(reviews.get("bypass_pull_request_allowances", {}).get(key) for key in ("users", "teams", "apps")):
        problems.append("remove pull request bypass allowances")
    for field in ("allow_force_pushes", "allow_deletions"):
        if (protection.get(field) or {}).get("enabled"):
            problems.append(f"disable {field}")
    rules = [line.split("#", 1)[0].split() for line in owners.splitlines() if line.split("#", 1)[0].strip()]
    if not rules or [part.lower() for part in rules[-1]] != ["*", "@" + manager.lower()]:
        problems.append(f"end the effective CODEOWNERS file with '* @{manager}' (sole owner for every file)")
    return problems


def check_repository(github: GitHub, repo: str, manager: str) -> dict[str, Any]:
    """Fail closed when protection cannot be verified. No settings are modified."""
    try:
        metadata: dict[str, Any] = github.api(f"repos/{repo}")
    except RateLimitError:
        raise
    except RuntimeError as error:
        raise RuntimeError(
            f"Host token cannot read repository {repo}. Check the repository name and, for a fine-grained token, "
            "its resource owner and selected repositories. Grant access to this project or save a replacement "
            "manager token with 'agent_worker github login --host'. " + str(error)
        ) from error
    if metadata.get("archived") or metadata.get("disabled"):
        raise RuntimeError(f"{repo}: repository is archived or disabled")
    try:
        permission = github.api(f"repos/{repo}/collaborators/{manager}/permission")["permission"]
    except RateLimitError:
        raise
    except RuntimeError as error:
        raise RuntimeError(f"Host could not verify @{manager}'s access to {repo}; check host token repository/Metadata access. " + str(error)) from error
    if permission not in ("admin", "write", "maintain"):
        raise RuntimeError(f"{repo}: manager needs write permission to be a code owner")
    try:
        branch_info = {item["name"]: item for item in github.items(f"repos/{repo}/branches")}
    except RateLimitError:
        raise
    except RuntimeError as error:
        raise RuntimeError(f"Host could not list branches for {repo}; the host token needs Contents read permission. " + str(error)) from error
    branches = {metadata["default_branch"]}
    branches.update(name for name in branch_info if name in ("main", "master"))
    for branch in sorted(branches):
        ref = quote(branch, safe="")
        if branch_info.get(branch, {}).get("protected") is False:
            raise RuntimeError(
                f"{repo}:{branch}: GitHub reports this branch is unprotected. "
                f"Create a classic branch protection rule for '{branch}' at https://github.com/{repo}/settings/branches "
                f"requiring pull requests, code owner approval and stale approval dismissal; "
                f"disable force pushes/deletions. "
                f"The effective CODEOWNERS file must end with '* @{manager}'. "
                "The host check verifies policy; it does not create protection rules."
            )
        try:
            protection = github.api(f"repos/{repo}/branches/{ref}/protection")
        except RateLimitError:
            raise
        except RuntimeError as error:
            raise RuntimeError(
                f"{repo}:{branch}: cannot read classic branch protection. "
                "A 404 can mean the classic rule is absent or the credential cannot read it. "
                f"Check https://github.com/{repo}/settings/branches for a rule matching '{branch}'; "
                "ruleset-only protection is not supported by this verifier. "
                "Authenticate a token on the host as the configured project manager with permission to read branch protection. " + str(error)
            ) from error
        # GitHub's contents API returns 404 for absent CODEOWNERS; distinguish that
        # from permission/network failures rather than silently skipping them.
        owners = ""
        for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
            try:
                content = github.api(f"repos/{repo}/contents/{path}?ref={ref}")
            except RateLimitError:
                raise
            except RuntimeError as error:
                if "HTTP 404" in str(error):
                    continue
                raise
            owners = base64.b64decode(content["content"]).decode("utf-8")
            break
        problems = protection_problems(protection, owners, manager)
        if problems:
            raise RuntimeError(f"{repo}:{branch}: " + "; ".join(problems))
    return metadata


class GitHubWorkflow:
    def __init__(self, vm: VM) -> None:
        self.vm = vm

    def upload(self) -> None:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            for name in ("github_workflow.py", "github_client.py", "git_credential.py", "github_review.py"):
                archive.add(GUEST_DIR / name, arcname=name)
        self.vm.ssh(
            'umask 077; mkdir -p "$HOME/.local/lib/agent-worker" && tar -xf - -C "$HOME/.local/lib/agent-worker" '
            '&& python3 "$HOME/.local/lib/agent-worker/github_workflow.py" bootstrap',
            input=payload.getvalue(),
        )

    def guest(self, *args: str, tty: bool = False) -> None:
        self.vm.ssh('"$HOME/.local/lib/agent-worker/github-venv/bin/python" "$HOME/.local/lib/agent-worker/github_workflow.py" ' + shlex.join(args), tty=tty)

    def config(self) -> dict[str, Any]:
        result = self.vm.ssh('"$HOME/.local/lib/agent-worker/github-venv/bin/python" "$HOME/.local/lib/agent-worker/github_workflow.py" config', capture=True)
        config: dict[str, Any] = json.loads(result.stdout)
        validate_identity(config["manager"])
        validate_identity(config["developer"])
        for repository in config["repositories"]:
            validate_repo(repository)
        return config

    @staticmethod
    def authenticate_manager(github: GitHub, config: dict[str, Any]) -> None:
        user = github.api("user")
        if user["login"].lower() != config["manager"].lower():
            raise RuntimeError(f"Host token must authenticate as project manager {config['manager']}, currently {user['login']}")

    def check(self) -> None:
        self.upload()
        config = self.config()
        github = GitHub()
        self.authenticate_manager(github, config)
        for repository in config["repositories"]:
            check_repository(github, repository, config["manager"])
            print(f"{repository}: full protection policy verified on the host")

    def configure(self, developer: str, manager: str, interval: int) -> None:
        validate_identity(developer)
        validate_identity(manager)
        self.upload()
        self.guest("configure", "--developer", developer, "--manager", manager, "--interval", str(interval))
        self.guest("install")
        self.vm.ssh('sudo -n loginctl enable-linger "$(id -un)"')
        print("Configured. Authenticate the machine account with: agent_worker github login")

    def login(self, host: bool = False, token_stdin: bool = False) -> None:
        if host:
            login = save_token(prompt_token(token_stdin))
            print(f"Host GitHub token saved for @{login}; github add/check verify the configured manager identity")
            return
        self.upload()
        if token_stdin:
            token = prompt_token(stdin=True)
            self.vm.ssh(
                '"$HOME/.local/lib/agent-worker/github-venv/bin/python" "$HOME/.local/lib/agent-worker/github_workflow.py" login --token-stdin',
                input=token + "\n",
            )
        else:
            self.guest("login", tty=True)

    def add(self, repository: str, invite: bool, developer: str | None) -> None:
        validate_repo(repository)
        if invite:
            if not developer:
                raise ValueError("--invite requires --developer MACHINE_ACCOUNT")
            validate_identity(developer)
        self.upload()
        config = self.config()
        if invite and developer and developer.lower() != config["developer"].lower():
            raise ValueError("--developer must match the configured machine account")
        github = GitHub()
        self.authenticate_manager(github, config)
        check_repository(github, repository, config["manager"])
        print(f"{repository}: full protection policy verified on the host")
        if invite:
            github.api(f"repos/{repository}/collaborators/{developer}", "PUT", {"permission": "push"})
            print(f"Invited @{developer} to {repository}")
        self.guest("add", repository)

    def action(self, action: str, **options: Any) -> None:
        if action == "restart-service":
            self.vm.ssh("systemctl --user restart agent-worker-github.service")
        elif action == "log":
            self.vm.ssh("journalctl --user -u agent-worker-github.service -f")
        elif action == "check":
            self.check()
        elif action == "run":
            self.guest("run", *(["--once"] if options.get("once") else []), tty=True)
        elif action == "retry":
            self.guest("retry", str(options["job"]))
        elif action == "remove":
            self.guest("remove", validate_repo(options["repository"]))
        else:
            self.guest(action)
