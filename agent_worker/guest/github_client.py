"""PyGithub adapter and token storage shared by the host and standalone guest."""

from __future__ import annotations

import getpass
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

DEPENDENCY = "PyGithub~=2.10.0"
TOKEN_PATH = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "agent-worker/github-token"
TOKEN_ENV = "AGENT_WORKER_GITHUB_TOKEN"


def read_token() -> str:
    token = os.environ.get(TOKEN_ENV) or (TOKEN_PATH.read_text().strip() if TOKEN_PATH.exists() else "")
    if not token or any(char.isspace() for char in token):
        raise RuntimeError("GitHub token missing or invalid. Run 'agent_worker github login --host' on the host or 'agent_worker github login' for the guest")
    return token


def prompt_token(stdin: bool = False) -> str:
    token = (sys.stdin.read() if stdin else getpass.getpass("GitHub personal access token: ")).strip()
    if not token or any(char.isspace() for char in token):
        raise ValueError("Pass a nonempty GitHub token without whitespace")
    return token


def save_token(token: str, expected: str | None = None) -> str:
    # Validate before replacing an existing working token. No token is returned
    # in configuration or included in errors, argv or git remote URLs.
    user = GitHub(token).api("user")
    login: str = user["login"]
    if expected and login.lower() != expected.lower():
        raise ValueError(f"Token belongs to @{login}; expected @{expected}")
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=TOKEN_PATH.parent, prefix=".github-token-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(token + "\n")
        Path(temporary).replace(TOKEN_PATH)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return login


class RateLimitError(RuntimeError):
    """No API calls may be made before the client's retry_at epoch."""


def snapshot(value: Any) -> dict[str, Any]:
    from github.GithubObject import GithubObject

    # List responses already contain the fields used by the workflow. Access the
    # public base property to avoid CompletableGithubObject.raw_data fetching each
    # object again (including invitation endpoints the developer cannot inspect).
    return GithubObject.raw_data.fget(value)  # type: ignore[attr-defined, no-any-return]


class GitHub:
    """Keep workflow snapshots as JSON while using PyGithub's public object API.

    Each call loads fresh objects so cached SDK properties cannot hide assignment,
    protection or review changes between the initial and pre-publication checks.
    PyGithub handles list pagination and typed HTTP exceptions.
    """

    def __init__(self, token: str | None = None) -> None:
        from github import Auth, Github

        self.client = Github(auth=Auth.Token(token or read_token()), per_page=100, timeout=30, retry=0)
        self.retry_at = 0.0
        self.rate_limits: dict[str, int] | None = None
        self.backoff = 0

    def api(self, path: str, method: str = "GET", body: Any = None, pages: bool = False) -> Any:
        from github import GithubException, RateLimitExceededException
        from requests import RequestException

        now = time.time()
        if self.rate_limits and self.rate_limits["remaining"] == 0 and self.rate_limits["reset"] > now:
            self.retry_at = max(self.retry_at, self.rate_limits["reset"] + 1)
        if self.retry_at > now:
            raise RateLimitError(f"GitHub API rate limited; retry after {self.retry_at:.0f} (in {self.retry_at - now:.0f}s)")
        try:
            result = self._api(path, method, body or {}, pages)
            remaining, limit = self.client.rate_limiting
            self.rate_limits = {"remaining": remaining, "limit": limit, "reset": self.client.rate_limiting_resettime}
            self.backoff = 0
            return result
        except GithubException as error:
            headers = {key.lower(): value for key, value in (error.headers or {}).items()}
            message = str((error.data or {}).get("message", "")) if isinstance(error.data, dict) else ""
            limited = error.status in (403, 429) and (
                isinstance(error, RateLimitExceededException)
                or error.status == 429
                or headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in headers
                or "rate limit" in message.lower()
            )
            if limited:
                now = time.time()  # Retry-After starts when the response arrives.

                def numeric(field: str) -> float:
                    try:
                        return float(headers.get(field, 0))
                    except (ValueError, TypeError):
                        return 0

                self.backoff = min(3600, max(60, self.backoff * 2))
                self.retry_at = max(now + self.backoff, now + numeric("retry-after") + 1, numeric("x-ratelimit-reset") + 1)
                raise RateLimitError(
                    f"GitHub API rate limited (HTTP {error.status}); retry after {self.retry_at:.0f} (in {self.retry_at - now:.0f}s)"
                ) from None
            # Identify the logical operation, excluding query strings, request
            # bodies and headers, which can contain task data or credentials.
            operation = urlsplit(path).path
            raise RuntimeError(
                f"GitHub operation {method} /{operation} failed (HTTP {error.status}); check token permissions and repository settings"
            ) from None
        except RequestException:
            raise RuntimeError("GitHub API connection failed; check connectivity and retry") from None

    def items(self, path: str) -> list[dict[str, Any]]:
        return self.api(path, pages=True)  # type: ignore[no-any-return]

    def _api(self, path: str, method: str, body: dict[str, Any], pages: bool) -> Any:
        parsed = urlsplit(path)
        parts = parsed.path.split("/")
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        if path == "user" and method == "GET":
            return self.client.get_user().raw_data
        if parts[:2] == ["user", "repository_invitations"]:
            user = self.client.get_user()
            if method == "GET" and len(parts) == 2 and pages:
                return [snapshot(invitation) for invitation in user.get_invitations()]
            if method == "PATCH" and len(parts) == 3:
                user.accept_invitation(int(parts[2]))
                return None
        if len(parts) < 3 or parts[0] != "repos":
            raise ValueError("Unsupported GitHub workflow operation")
        tail = parts[3:]
        repo = self.client.get_repo("/".join(parts[1:3]))
        if not tail and method == "GET":
            return snapshot(repo)
        if tail[:1] == ["collaborators"]:
            if method == "GET" and tail[2:] == ["permission"]:
                return {"permission": repo.get_collaborator_permission(tail[1])}
            if method == "PUT" and len(tail) == 2:
                invitation = repo.add_to_collaborators(tail[1], permission=body["permission"])
                return snapshot(invitation) if invitation else None
        if tail[:1] == ["branches"] and method == "GET":
            if len(tail) == 1 and pages:
                return [snapshot(branch) for branch in repo.get_branches()]
            if len(tail) == 3 and tail[2] == "protection":
                return snapshot(repo.get_branch(unquote(tail[1])).get_protection())
        if tail[:1] == ["contents"] and method == "GET":
            content = repo.get_contents(unquote("/".join(tail[1:])), ref=query["ref"])
            if isinstance(content, list):
                raise ValueError("Expected a file from GitHub")
            return snapshot(content)
        if tail[:1] == ["issues"]:
            if len(tail) == 1 and pages and method == "GET":
                return [snapshot(issue) for issue in repo.get_issues(state=query["state"], assignee=query["assignee"])]
            issue = repo.get_issue(int(tail[1]))
            if len(tail) == 2 and method == "GET":
                return snapshot(issue)
            if tail[2:] == ["timeline"] and method == "GET" and pages:
                return [snapshot(event) for event in issue.get_timeline()]
            if tail[2:] == ["comments"]:
                if method == "GET" and pages:
                    return [snapshot(comment) for comment in issue.get_comments()]
                if method == "POST":
                    return snapshot(issue.create_comment(body["body"]))
        if tail[:1] == ["pulls"]:
            if len(tail) == 1:
                if method == "GET" and pages:
                    options = {"state": query.get("state", "open")}
                    if "head" in query:
                        options["head"] = query["head"]
                    return [snapshot(pull) for pull in repo.get_pulls(**options)]
                if method == "POST":
                    return snapshot(repo.create_pull(title=body["title"], body=body["body"], head=body["head"], base=body["base"]))
            pull = repo.get_pull(int(tail[1]))
            if method == "GET" and len(tail) == 2:
                return pull.raw_data
            if method == "GET" and pages:
                if tail[2:] == ["files"]:
                    return [snapshot(file) for file in pull.get_files()]
                if tail[2:] == ["reviews"]:
                    return [snapshot(review) for review in pull.get_reviews()]
                if tail[2:] == ["comments"]:
                    return [snapshot(comment) for comment in pull.get_review_comments()]
            if method == "POST" and tail[2:] == ["reviews"]:
                return snapshot(pull.create_review(commit=repo.get_commit(body["commit_id"]), body=body["body"], event="COMMENT", comments=body["comments"]))
            if method == "POST" and tail[2:] == ["requested_reviewers"]:
                pull.create_review_request(reviewers=body["reviewers"])
                return None
        raise ValueError("Unsupported GitHub workflow operation")
