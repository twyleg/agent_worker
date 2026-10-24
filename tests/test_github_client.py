from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, cast
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from github import Auth, Github

from agent_worker import github as host
from agent_worker.guest import github_client as client
from agent_worker.guest import github_workflow as workflow
from agent_worker.guest import git_credential
from agent_worker.main import Application


@pytest.fixture
def token_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "credentials/github-token"
    monkeypatch.setattr(client, "TOKEN_PATH", path)
    monkeypatch.delenv(client.TOKEN_ENV, raising=False)
    return path


@pytest.fixture
def api_server() -> Iterator[tuple[str, list[tuple[str, str, Any, str | None]]]]:
    requests: list[tuple[str, str, Any, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def respond(self) -> None:
            base = f"http://127.0.0.1:{cast(ThreadingHTTPServer, self.server).server_port}"
            parsed = urlsplit(self.path)
            path = parsed.path
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or "null")
            requests.append((self.command, self.path, data, self.headers.get("Authorization")))
            status = 200
            result: Any = None
            link: str | None = None
            repo_url = base + "/repos/owner/project"
            if path == "/user":
                result = {"login": "developer", "id": 123, "url": base + "/user"}
            elif path == "/repos/owner/project":
                result = {"id": 1, "url": repo_url, "full_name": "owner/project", "default_branch": "main", "permissions": {"push": True}}
            elif path == "/repos/owner/project/branches":
                page = parse_qs(parsed.query).get("page", ["1"])[0]
                name = "main" if page == "1" else "master"
                result = [{"name": name, "protected": True}]
                if page == "1":
                    link = f'<{repo_url}/branches?page=2>; rel="next"'
            elif path == "/repos/owner/project/branches/main":
                result = {"name": "main", "protected": True, "protection_url": repo_url + "/branches/main/protection"}
            elif path == "/repos/owner/project/branches/main/protection":
                result = {"enforce_admins": {"enabled": True}}
            elif path == "/repos/owner/project/contents/CODEOWNERS":
                result = {"type": "file", "path": "CODEOWNERS", "content": "KiBAbWFuYWdlcgo=", "encoding": "base64"}
            elif path in ("/repos/owner/project/issues/1", "/repos/owner/project/issues/2"):
                number = int(path.rsplit("/", 1)[1])
                result = {"id": number, "number": number, "url": repo_url + f"/issues/{number}", "state": "open", "title": "Task"}
            elif path == "/repos/owner/project/issues/2/timeline":
                result = [{"id": 77, "event": "review_requested", "requested_reviewer": {"login": "developer"}}]
            elif path == "/repos/owner/project/commits/" + "a" * 40:
                result = {"sha": "a" * 40, "url": repo_url + "/commits/" + "a" * 40}
            elif path == "/repos/owner/project/issues/1/comments":
                comment = {"id": 5, "body": "Question", "url": repo_url + "/issues/comments/5"}
                result = [comment] if self.command == "GET" else dict(comment, body=data["body"])
            elif path == "/repos/owner/project/pulls":
                if self.command == "GET":
                    result = [{"number": 2, "url": repo_url + "/pulls/2"}]
                else:
                    result, status = dict(data, number=2, url=repo_url + "/pulls/2"), 201
            elif path == "/repos/owner/project/pulls/2":
                result = {"number": 2, "url": repo_url + "/pulls/2", "title": "Human PR", "head": {"sha": "a" * 40}}
            elif path.endswith("/requested_reviewers"):
                result = {}
            elif path == "/user/repository_invitations":
                result = [{"id": 7, "repository": {"full_name": "owner/project"}}]
            elif path == "/user/repository_invitations/7":
                status = 204
            elif path == "/repos/owner/project/collaborators/manager/permission":
                result = {"permission": "admin"}
            elif path == "/repos/owner/project/collaborators/developer":
                status = 204
            elif path == "/repos/owner/project/issues":
                result = [{"id": 1, "number": 1}]
            elif path == "/repos/owner/project/pulls/2/files":
                result = [{"filename": "hello.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+print('Hello')"}]
            elif path == "/repos/owner/project/pulls/2/reviews":
                result = [{"id": 3, "state": "CHANGES_REQUESTED"}] if self.command == "GET" else dict(data, id=88, state="COMMENTED")
            elif path == "/repos/owner/project/pulls/2/comments":
                result = [{"id": 4, "path": "test.py", "line": 2}]
            else:
                status, result = 404, {"message": "Not Found with test-token-sensitive"}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-RateLimit-Limit", "5000")
            self.send_header("X-RateLimit-Remaining", "4999")
            self.send_header("X-RateLimit-Reset", "2000000000")
            if link:
                self.send_header("Link", link)
            self.end_headers()
            if status != 204:
                self.wfile.write(json.dumps(result).encode())

        do_GET = do_POST = do_PATCH = do_PUT = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def adapter(base: str) -> client.GitHub:
    instance = client.GitHub("test-token-sensitive")
    instance.client.close()
    instance.client = Github(
        auth=Auth.Token("test-token-sensitive"), base_url=base, per_page=100, retry=0, seconds_between_requests=0, seconds_between_writes=0
    )
    return instance


def test_real_pygithub_pagination_and_workflow_operations(api_server: Any) -> None:
    base, requests = api_server
    github = adapter(base)
    try:
        assert github.api("user")["login"] == "developer"
        assert [branch["name"] for branch in github.items("repos/owner/project/branches")] == ["main", "master"]
        assert github.api("repos/owner/project/branches/main/protection")["enforce_admins"]["enabled"]
        assert github.api("repos/owner/project/contents/CODEOWNERS?ref=main")["content"] == "KiBAbWFuYWdlcgo="
        assert github.api("repos/owner/project/issues/1")["title"] == "Task"
        assert github.items("repos/owner/project/issues/1/comments")[0]["id"] == 5
        message = "Markdown\n`literal` $value"
        assert github.api("repos/owner/project/issues/1/comments", "POST", {"body": message})["body"] == message
        assert github.items("repos/owner/project/issues?state=open&assignee=developer")[0]["number"] == 1
        assert github.items("repos/owner/project/pulls?state=all&head=owner%3Aagent-worker%2Fissue-1")[0]["number"] == 2
        assert github.items("repos/owner/project/pulls/2/reviews")[0]["state"] == "CHANGES_REQUESTED"
        assert github.items("repos/owner/project/pulls/2/comments")[0]["path"] == "test.py"
        pull = github.api("repos/owner/project/pulls", "POST", {"title": "Feature", "body": "Closes #1", "head": "agent-worker/issue-1", "base": "main"})
        assert pull["number"] == 2
        github.api("repos/owner/project/pulls/2/requested_reviewers", "POST", {"reviewers": ["manager"]})
        assert github.api("repos/owner/project/collaborators/manager/permission")["permission"] == "admin"
        github.api("repos/owner/project/collaborators/developer", "PUT", {"permission": "push"})
        assert github.items("user/repository_invitations")[0]["id"] == 7
        github.api("user/repository_invitations/7", "PATCH")
        assert all(auth == "token test-token-sensitive" for _, _, _, auth in requests)
        assert github.rate_limits == {"remaining": 4999, "limit": 5000, "reset": 2000000000}
        assert any(method == "POST" and body == {"reviewers": ["manager"]} for method, _, body, _ in requests)
    finally:
        github.client.close()


def test_sdk_404_is_preserved_without_response_or_token_leak(api_server: Any) -> None:
    base, _ = api_server
    github = adapter(base)
    try:
        with pytest.raises(RuntimeError, match="HTTP 404") as error:
            github.api("repos/owner/project/contents/.github/CODEOWNERS?ref=main")
        assert "test-token-sensitive" not in str(error.value)
        assert "Not Found with" not in str(error.value)
        assert "GET /repos/owner/project/contents/.github/CODEOWNERS" in str(error.value)
        assert "?ref=" not in str(error.value)
    finally:
        github.client.close()


def test_tokens_are_validated_and_written_privately(token_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    github = Mock()
    github.api.return_value = {"login": "developer"}
    monkeypatch.setattr(client, "GitHub", lambda token: github)
    assert client.save_token("new-token", expected="developer") == "developer"
    assert token_path.read_text() == "new-token\n"
    assert token_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="expected @manager"):
        client.save_token("wrong-account", expected="manager")
    assert token_path.read_text() == "new-token\n"
    github.api.side_effect = RuntimeError("GitHub API request failed (HTTP 401)")
    with pytest.raises(RuntimeError):
        client.save_token("expired-token")
    assert token_path.read_text() == "new-token\n"


def test_token_sources_and_stdin_validation(token_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="token missing"):
        client.read_token()
    token_path.parent.mkdir()
    token_path.write_text("file-token\n")
    assert client.read_token() == "file-token"
    monkeypatch.setenv(client.TOKEN_ENV, "environment-token")
    assert client.read_token() == "environment-token"
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin-token\n"))
    assert client.prompt_token(stdin=True) == "stdin-token"
    monkeypatch.setattr(sys, "stdin", io.StringIO("bad\ntoken"))
    with pytest.raises(ValueError):
        client.prompt_token(stdin=True)


@pytest.mark.parametrize(
    "protocol, hostname, operation, expected",
    [
        ("https", "github.com", "get", True),
        ("https", "evil.test", "get", False),
        ("http", "github.com", "get", False),
        ("https", "github.com", "store", False),
        ("https", "github.com", "erase", False),
    ],
)
def test_git_helper_only_returns_token_to_github_https(
    token_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str, hostname: str, operation: str, expected: bool
) -> None:
    token_path.parent.mkdir()
    token_path.write_text("guest-secret\n")
    monkeypatch.setattr(sys, "argv", ["git_credential.py", operation])
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"protocol={protocol}\nhost={hostname}\n\n"))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        git_credential.main()
    assert ("password=guest-secret" in output.getvalue()) is expected
    assert not expected or "username=x-access-token" in output.getvalue()


def test_git_command_uses_ephemeral_helper_without_token_in_argv(token_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(workflow.subprocess, "run", run)
    monkeypatch.setenv(client.TOKEN_ENV, "guest-secret")
    workflow.command("git", "push", "origin", "HEAD:refs/heads/agent-worker/issue-1")
    args = run.call_args.args[0]
    env = run.call_args.kwargs["env"]
    assert "guest-secret" not in str(args)
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert "git_credential.py" in env["GIT_CONFIG_VALUE_1"]
    assert "guest-secret" not in env["GIT_CONFIG_VALUE_1"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_real_git_credential_fill_does_not_invoke_old_gh_helper(token_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(client.TOKEN_ENV, "guest-secret")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text('[credential "https://github.com"]\n helper = "!gh auth git-credential"\n')
    output = workflow.command("git", "credential", "fill", input="protocol=https\nhost=github.com\n\n")
    assert "password=guest-secret" in output


def test_host_token_login_stays_on_host(monkeypatch: pytest.MonkeyPatch) -> None:
    vm = Mock()
    monkeypatch.setattr(host, "prompt_token", lambda stdin: "manager-token")
    save = Mock(return_value="manager")
    monkeypatch.setattr(host, "save_token", save)
    host.GitHubWorkflow(vm).login(host=True, token_stdin=True)
    save.assert_called_once_with("manager-token")
    vm.ssh.assert_not_called()


def test_guest_token_stdin_travels_only_through_ssh_input(monkeypatch: pytest.MonkeyPatch) -> None:
    vm = Mock()
    monkeypatch.setattr(host, "prompt_token", lambda stdin: "developer-token")
    host.GitHubWorkflow(vm).login(token_stdin=True)
    call = vm.ssh.call_args
    assert "developer-token" not in call.args[0]
    assert call.kwargs["input"] == "developer-token\n"
    assert "--token-stdin" in call.args[0]


def test_login_parser_supports_separate_host_and_guest_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = Mock()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agent_worker.main.GitHubWorkflow", lambda vm: controller)
    assert Application().start(["github", "login", "--host", "--token-stdin"]) == 0
    controller.login.assert_called_once_with(True, True)


def test_standalone_helper_help_needs_no_installed_python_packages() -> None:
    result = subprocess.run([sys.executable, "-S", str(Path(workflow.__file__)), "--help"], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "bootstrap" in result.stdout and "login" in result.stdout


def test_guest_service_uses_the_virtualenv_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(workflow, "CONFIG", tmp_path / ".config/agent-worker/github.json")
    run = Mock(return_value="")
    monkeypatch.setattr(workflow, "command", run)
    workflow.main(["install"])
    unit = (tmp_path / ".config/systemd/user/agent-worker-github.service").read_text()
    assert f'ExecStart="{sys.executable}"' in unit
    assert "KillMode=control-group" in unit
    run.assert_called_once_with("systemctl", "--user", "daemon-reload")


@pytest.mark.parametrize(
    "status, headers, message, minimum_wait",
    [
        (429, {"Retry-After": "120"}, "slow down", 120),
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "2000000000"}, "API rate limit exceeded", 60),
        (403, {}, "secondary rate limit: private-body", 60),
    ],
)
def test_rate_limit_cooldown_prevents_further_requests(
    monkeypatch: pytest.MonkeyPatch, status: int, headers: dict[str, str], message: str, minimum_wait: int
) -> None:
    import time
    from github import GithubException

    github = client.GitHub("synthetic-token")
    request = Mock(side_effect=GithubException(status, {"message": message}, headers))
    monkeypatch.setattr(github, "_api", request)
    started = time.time()
    with pytest.raises(client.RateLimitError) as error:
        github.api("user")
    assert github.retry_at >= started + minimum_wait
    assert "private-body" not in str(error.value)
    with pytest.raises(client.RateLimitError):
        github.api("repos/owner/project")
    assert request.call_count == 1
    if "x-ratelimit-reset" in headers:
        assert github.retry_at >= 2000000001
    github.client.close()


def test_permission_denied_is_not_treated_as_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from github import GithubException

    github = client.GitHub("synthetic-token")
    monkeypatch.setattr(github, "_api", Mock(side_effect=GithubException(403, {"message": "Resource not accessible"}, {})))
    with pytest.raises(RuntimeError) as error:
        github.api("user")
    assert not isinstance(error.value, client.RateLimitError)
    assert github.retry_at == 0
    github.client.close()


def test_exhausted_budget_defers_before_making_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    github = client.GitHub("synthetic-token")
    github.rate_limits = {"remaining": 0, "limit": 5000, "reset": 2000000000}
    request = Mock()
    monkeypatch.setattr(github, "_api", request)
    with pytest.raises(client.RateLimitError):
        github.api("user")
    request.assert_not_called()
    assert github.retry_at == 2000000001
    github.client.close()


def test_successive_secondary_limits_increase_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    from github import GithubException

    github = client.GitHub("synthetic-token")
    monkeypatch.setattr(github, "_api", Mock(side_effect=GithubException(403, {"message": "secondary rate limit"}, {})))
    with pytest.raises(client.RateLimitError):
        github.api("user")
    assert github.backoff == 60
    github.retry_at = 0  # Simulate the first cooldown having elapsed.
    with pytest.raises(client.RateLimitError):
        github.api("user")
    assert github.backoff == 120
    github.client.close()


def test_retry_after_is_measured_from_response_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from github import GithubException

    github = client.GitHub("synthetic-token")
    error = GithubException(429, {"message": "rate limit"}, {"retry-after": "120"})
    monkeypatch.setattr(github, "_api", Mock(side_effect=error))
    monkeypatch.setattr(client.time, "time", Mock(side_effect=[1000, 1020]))
    with pytest.raises(client.RateLimitError):
        github.api("user")
    assert github.retry_at >= 1140
    github.client.close()


def test_sdk_review_requests_timeline_files_and_commit_pinned_review(api_server: Any) -> None:
    base, requests = api_server
    github = adapter(base)
    try:
        assert github.items("repos/owner/project/pulls?state=open")[0]["number"] == 2
        assert github.api("repos/owner/project/pulls/2")["head"]["sha"] == "a" * 40
        assert github.items("repos/owner/project/issues/2/timeline")[0]["requested_reviewer"]["login"] == "developer"
        assert github.items("repos/owner/project/pulls/2/files")[0]["filename"] == "hello.py"
        comment = {"path": "hello.py", "line": 1, "side": "RIGHT", "body": "A finding"}
        published = github.api("repos/owner/project/pulls/2/reviews", "POST", {"commit_id": "a" * 40, "body": "Review summary", "comments": [comment]})
        assert published["state"] == "COMMENTED"
        payload = next(body for method, path, body, _ in requests if method == "POST" and path.endswith("/reviews"))
        assert payload == {"commit_id": "a" * 40, "body": "Review summary", "event": "COMMENT", "comments": [comment]}
    finally:
        github.client.close()
