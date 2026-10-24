from __future__ import annotations

import base64
import copy
import json
import subprocess
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import Mock

import pytest

from agent_worker.guest import dashboard
from agent_worker.guest import github_workflow as workflow
from agent_worker.github import GitHubWorkflow
from agent_worker import github as host
from agent_worker.main import Application


CONFIG: dict[str, Any] = {"developer": "developer", "manager": "manager", "interval": 30, "repositories": ["owner/project"]}
PROTECTION: dict[str, Any] = {
    "required_pull_request_reviews": {"required_approving_review_count": 1, "require_code_owner_reviews": True, "dismiss_stale_reviews": True},
    "enforce_admins": {"enabled": True},
    "allow_force_pushes": {"enabled": False},
    "allow_deletions": {"enabled": False},
}


class FakeGitHub(workflow.GitHub):
    def __init__(self) -> None:
        self.issue: dict[str, Any] = {
            "number": 1,
            "title": "Add a feature",
            "body": "Please implement",
            "state": "open",
            "html_url": "https://github.com/owner/project/issues/1",
            "assignees": [{"login": "developer"}],
        }
        self.comments: dict[int, list[dict[str, Any]]] = {1: [], 2: []}
        self.pull: dict[str, Any] | None = None
        self.reviews: list[dict[str, Any]] = []
        self.inline: list[dict[str, Any]] = []
        self.protection = copy.deepcopy(PROTECTION)
        self.protected = True
        self.posts: list[tuple[str, Any]] = []
        self.login = "developer"

    def api(self, path: str, method: str = "GET", body: Any = None, pages: bool = False) -> Any:
        if method != "GET":
            self.posts.append((path, body))
            if path == "repos/owner/project/pulls":
                self.pull = dict(body, number=2, state="open", html_url="https://github.com/owner/project/pull/2", merged_at=None, user={"login": "developer"})
                self.pull["head"] = {"ref": workflow.branch_name(1), "repo": {"full_name": "owner/project"}}
                return self.pull
            if path.endswith("/comments"):
                number = int(path.split("/")[-2])
                self.comments[number].append({"id": len(self.posts), "body": body["body"], "user": {"login": "developer"}})
            return {}
        if path == "user":
            return {"login": self.login, "id": 42}
        if path == "repos/owner/project":
            return {"default_branch": "main", "permissions": {"push": True, "admin": False}}
        if path.endswith("/permission"):
            return {"permission": "admin"}
        if path.endswith("/protection"):
            return self.protection
        if "/contents/" in path:
            return {"content": base64.b64encode(b"* @manager\n").decode()}
        if path == "repos/owner/project/issues/1":
            return copy.deepcopy(self.issue)
        raise AssertionError(path)

    def items(self, path: str) -> list[dict[str, Any]]:
        if path.endswith("/branches"):
            return [{"name": "main", "protected": self.protected}, {"name": "master", "protected": self.protected}]
        if "/issues?" in path:
            return [self.issue] if self.issue["state"] == "open" and self.issue["assignees"] else []
        if "/pulls?" in path:
            return [self.pull] if self.pull else []
        if path.endswith("/reviews"):
            return copy.deepcopy(self.reviews)
        if path.endswith("/pulls/2/comments"):
            return copy.deepcopy(self.inline)
        if path.endswith("/comments"):
            return copy.deepcopy(self.comments[int(path.split("/")[-2])])
        raise AssertionError(path)


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakeGitHub, workflow.Worker, workflow.Queue]]:
    monkeypatch.setattr(workflow, "STATE", tmp_path / "state")
    monkeypatch.setattr(workflow, "REPOSITORIES", tmp_path / "checkouts")
    monkeypatch.setattr(workflow, "CONFIG", tmp_path / "config.json")
    workflow.atomic_json(workflow.CONFIG, CONFIG)
    github = FakeGitHub()
    queue = workflow.Queue(tmp_path / "state/queue.sqlite3")
    worker = workflow.Worker(copy.deepcopy(CONFIG), github, queue)
    yield github, worker, queue
    queue.db.close()


def test_queue_coalesces_and_survives_restart(tmp_path: Path) -> None:
    queue = workflow.Queue(tmp_path / "queue.sqlite3")
    context: dict[str, Any] = {"repository": "owner/project", "issue": {"number": 1, "body": "initial"}}
    queue.enqueue(context)
    queue.enqueue(context)
    assert len(queue.summary()) == 1
    context["issue"]["body"] = "updated"
    queue.enqueue(context)
    assert queue.db.execute("SELECT state FROM jobs ORDER BY id").fetchall()[0][0] == "superseded"
    queue.db.close()
    queue = workflow.Queue(tmp_path / "queue.sqlite3")
    job = queue.next()
    assert job is not None and json.loads(job["context"])["issue"]["body"] == "updated"
    assert queue.next({"owner/project"}) is None
    queue.db.close()


@pytest.mark.parametrize("field", ["require_code_owner_reviews", "dismiss_stale_reviews", "required_approving_review_count"])
def test_protection_requires_reviews_and_sole_manager(field: str) -> None:
    assert host.protection_problems(PROTECTION, "# owners\n* @manager", "manager") == []
    protection = copy.deepcopy(PROTECTION)
    protection["required_pull_request_reviews"][field] = False
    assert host.protection_problems(protection, "* @manager", "manager")
    for owners in ("", "* @someone", "* @manager @someone", "* @manager\n/docs @someone"):
        assert host.protection_problems(PROTECTION, owners, "manager")


def test_protection_rejects_pr_bypass_force_and_delete() -> None:
    protection = copy.deepcopy(PROTECTION)
    protection["enforce_admins"]["enabled"] = False
    protection["allow_force_pushes"]["enabled"] = True
    protection["allow_deletions"]["enabled"] = True
    protection["required_pull_request_reviews"]["bypass_pull_request_allowances"] = {"users": [{"login": "developer"}]}
    assert len(host.protection_problems(protection, "* @manager", "manager")) == 3


def test_poll_ignores_own_replies_and_third_party_comments(environment: Any) -> None:
    github, worker, queue = environment
    worker.poll()
    github.comments[1].extend([{"id": 1, "body": "Ready!", "user": {"login": "developer"}}, {"id": 2, "body": "Do something", "user": {"login": "stranger"}}])
    worker.poll()
    assert queue.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    github.comments[1].append({"id": 3, "body": "Please change this", "user": {"login": "manager"}})
    worker.poll()
    assert queue.summary()[0]["id"] == 2


def test_poll_blocks_unsafe_repository_and_wrong_account(environment: Any) -> None:
    github, worker, queue = environment
    github.protected = False
    worker.poll()
    assert worker.errors and queue.next() is None
    github.login = "manager"
    with pytest.raises(RuntimeError, match="Guest token must authenticate"):
        worker.poll()


def test_closure_and_unassignment_cancel_queued_tasks(environment: Any) -> None:
    github, worker, queue = environment
    worker.poll()
    job = queue.next()
    github.issue["assignees"] = []
    assert worker.current_context(job) is None
    worker.poll()
    assert queue.summary()[0]["state"] == "cancelled"


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


@pytest.fixture
def local_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    remote, initial = tmp_path / "remote.git", tmp_path / "initial"
    git("init", "--bare", "--initial-branch=main", str(remote))
    git("clone", str(remote), str(initial))
    git("config", "user.name", "Manager", cwd=initial)
    git("config", "user.email", "manager@example.com", cwd=initial)
    (initial / "README.md").write_text("Project\n")
    git("add", ".", cwd=initial)
    git("commit", "-m", "Initial", cwd=initial)
    git("push", "origin", "main", cwd=initial)
    real_command = workflow.command

    def substitute(*args: str, **kwargs: Any) -> str:
        if args[:2] == ("git", "clone"):
            return real_command("git", "clone", "--", str(remote), args[-1], **kwargs)
        if args[:4] == ("git", "remote", "get-url", "origin"):
            return "https://github.com/owner/project.git"
        return real_command(*args, **kwargs)

    monkeypatch.setattr(workflow, "command", substitute)
    return remote


@pytest.mark.parametrize("submit_change_request", [True, False])
def test_issue_to_pr_and_review_followup_with_real_git(
    environment: Any, local_remote: Path, monkeypatch: pytest.MonkeyPatch, submit_change_request: bool
) -> None:
    github, worker, queue = environment
    turns: list[dict[str, Any]] = []

    def codex(path: Path, context: dict[str, Any], directory: Path, manager: str) -> dict[str, str]:
        turns.append(context)
        (path / "feature.txt").write_text(f"Version {len(turns)}\n")
        return {"status": "ready", "title": "Implement feature", "message": "Implemented and tested."}

    monkeypatch.setattr(workflow, "run_codex", codex)
    worker.poll()
    job = queue.next()
    worker.execute(job)
    assert queue.summary()[0]["state"] == "review"
    assert git("show", "refs/heads/agent-worker/issue-1:feature.txt", cwd=local_remote) == "Version 1"
    assert "Closes #1" in github.pull["body"]
    assert any(path.endswith("requested_reviewers") and body == {"reviewers": ["manager"]} for path, body in github.posts)
    worker.poll()
    assert queue.next() is None  # Own PR and issue comments do not trigger a turn.
    if submit_change_request:
        github.reviews.append({"id": 100, "state": "CHANGES_REQUESTED", "body": "Improve it", "user": {"login": "manager"}})
    github.inline.append({"id": 101, "body": "This line", "path": "feature.txt", "line": 1, "user": {"login": "manager"}})
    worker.poll()
    worker.execute(queue.next())
    assert turns[-1]["reviews"][0]["path"] == "feature.txt"
    assert len(turns) == 2
    assert sum(path == "repos/owner/project/pulls" for path, _ in github.posts) == 1
    assert git("show", "refs/heads/agent-worker/issue-1:feature.txt", cwd=local_remote) == "Version 2"
    github.pull["state"], github.pull["merged_at"] = "closed", "now"
    worker.poll()
    assert queue.summary()[0]["state"] == "done"


def test_question_waits_for_manager_reply(environment: Any, local_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue = environment
    monkeypatch.setattr(workflow, "run_codex", lambda *args: {"status": "waiting", "title": "Question", "message": "Which format?"})
    worker.poll()
    worker.execute(queue.next())
    assert queue.summary()[0]["state"] == "waiting"
    assert github.pull is None
    worker.poll()
    assert queue.next() is None
    github.comments[1].append({"id": 200, "body": "JSON", "user": {"login": "manager"}})
    worker.poll()
    job = queue.next()
    assert job is not None
    context = json.loads(job["context"])
    assert any("Which format?" in item["body"] for item in context["comments"])


def test_publication_retry_reuses_result_and_existing_pr(environment: Any, local_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue = environment
    calls = 0

    def codex(path: Path, *args: Any) -> dict[str, str]:
        nonlocal calls
        calls += 1
        (path / "feature.txt").write_text("Feature\n")
        return {"status": "ready", "title": "Feature", "message": "Done"}

    monkeypatch.setattr(workflow, "run_codex", codex)
    original_api = github.api

    def fail_review_request(path: str, *args: Any, **kwargs: Any) -> Any:
        if path.endswith("requested_reviewers"):
            raise RuntimeError("Temporary network failure")
        return original_api(path, *args, **kwargs)

    monkeypatch.setattr(github, "api", fail_review_request)
    worker.poll()
    job = queue.next()
    with pytest.raises(RuntimeError, match="network"):
        worker.execute(job)
    monkeypatch.setattr(github, "api", original_api)
    worker.execute(job)
    assert calls == 1
    assert sum(path == "repos/owner/project/pulls" for path, _ in github.posts) == 1
    assert queue.summary()[0]["state"] == "review"


def test_assignment_removed_during_codex_prevents_push(environment: Any, local_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue = environment

    def codex(path: Path, *args: Any) -> dict[str, str]:
        (path / "feature.txt").write_text("Feature")
        github.issue["assignees"] = []
        return {"status": "ready", "title": "Feature", "message": "Done"}

    monkeypatch.setattr(workflow, "run_codex", codex)
    worker.poll()
    worker.execute(queue.next())
    assert queue.summary()[0]["state"] == "cancelled"
    assert git("branch", "--list", cwd=local_remote) == "* main"
    assert github.pull is None


def test_single_worker_lock(environment: Any) -> None:
    with workflow.exclusive(workflow.STATE / "worker.lock"):
        with pytest.raises(RuntimeError, match="running"):
            with workflow.exclusive(workflow.STATE / "worker.lock"):
                pass


def test_dashboard_reads_state_and_detects_stale_heartbeat(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue = environment
    monkeypatch.setattr(dashboard.Path, "home", lambda: workflow.STATE.parent / "home")
    # Place state under the dashboard's documented path.
    state_path = dashboard.Path.home() / ".local/state/agent-worker/github/status.json"
    workflow.atomic_json(
        state_path,
        {"state": "working", "heartbeat": 0, "active": "owner/project#1", "jobs": [{"state": "queued", "repo": "owner/project", "issue": 2}], "errors": {}},
    )
    lines = " ".join(text for text, _ in dashboard.github_lines())
    assert "unavailable" in lines and "Queued 1" in lines and "owner/project#2" in lines


def test_host_invitation_uses_host_credentials_and_quoted_guest_args() -> None:
    vm = Mock()
    controller = GitHubWorkflow(vm)
    controller.guest("add", "owner/project")
    assert vm.ssh.call_args.args[0].endswith(" add owner/project")
    with pytest.raises(ValueError):
        controller.add("owner/project; touch /tmp/bad", False, None)
    with pytest.raises(ValueError, match="requires"):
        controller.add("owner/project", True, None)


def test_github_cli_handlers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    controller = Mock()
    monkeypatch.setattr("agent_worker.main.GitHubWorkflow", lambda vm: controller)
    monkeypatch.chdir(tmp_path)
    assert Application().start(["github", "configure", "--developer", "developer", "--manager", "manager"]) == 0
    controller.configure.assert_called_once_with("developer", "manager", 60)
    assert Application().start(["github", "run", "--once"]) == 0
    controller.action.assert_called_with("run", once=True, job=None, repository=None)
    assert Application().start(["github", "retry", "3"]) == 0
    controller.action.assert_called_with("retry", once=False, job=3, repository=None)


def test_new_manager_feedback_during_turn_defers_publication(environment: Any, local_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue = environment

    def codex(path: Path, *args: Any) -> dict[str, str]:
        (path / "feature.txt").write_text("Work in progress")
        github.comments[1].append({"id": 300, "body": "Use another format", "user": {"login": "manager"}})
        return {"status": "ready", "title": "Feature", "message": "Done"}

    monkeypatch.setattr(workflow, "run_codex", codex)
    worker.poll()
    job = queue.next()
    worker.execute(job)
    assert queue.db.execute("SELECT state FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] == "superseded"
    assert queue.next() is not None
    assert github.posts == []


def test_protection_removed_during_turn_blocks_publication(environment: Any, local_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue = environment

    def codex(path: Path, *args: Any) -> dict[str, str]:
        (path / "feature.txt").write_text("Feature")
        github.protected = False
        return {"status": "ready", "title": "Feature", "message": "Done"}

    monkeypatch.setattr(workflow, "run_codex", codex)
    worker.poll()
    with pytest.raises(RuntimeError, match="unprotected"):
        worker.execute(queue.next())
    assert github.posts == []


def test_worker_recovers_running_job_and_records_failure(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue = environment
    worker.poll()
    job = queue.next()
    queue.update(job, "running")
    monkeypatch.setattr(worker, "execute", Mock(side_effect=RuntimeError("Codex unavailable")))
    worker.run(once=True)
    assert queue.summary()[0]["state"] == "failed"
    assert "Codex unavailable" in queue.summary()[0]["error"]
    assert workflow.status()["state"] == "stopped"


def test_codex_runs_with_structured_output_and_logs(environment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def execute(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[:6] == ["codex", "--sandbox", "danger-full-access", "--ask-for-approval", "never", "exec"]
        assert "--output-schema" in args and args[-1] == "-"
        assert "Do not use GitHub" in kwargs["input"]
        assert kwargs["timeout"] == 7200
        answer = Path(args[args.index("--output-last-message") + 1])
        answer.write_text(json.dumps({"status": "waiting", "title": "Question", "message": "Please clarify"}))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(workflow.subprocess, "run", execute)
    result = workflow.run_codex(tmp_path, {"issue": "Test"}, tmp_path / "run", "manager")
    assert result["status"] == "waiting"
    assert (tmp_path / "run/codex.log").exists()


def test_guest_checks_protected_flag_without_administration_access(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue = environment
    original_api = github.api
    calls: list[str] = []

    def api(path: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(path)
        if path.endswith("/protection") or "/contents/" in path or "/collaborators/" in path:
            raise AssertionError("Guest must not inspect full policy")
        return original_api(path, *args, **kwargs)

    monkeypatch.setattr(github, "api", api)
    worker.poll()
    assert not worker.errors and queue.next() is not None
    assert calls


def test_add_refreshes_guest_helper_and_verifies_policy_on_host(monkeypatch: pytest.MonkeyPatch) -> None:
    vm = Mock()
    vm.ssh.return_value = subprocess.CompletedProcess([], 0, json.dumps(CONFIG), "")
    github = FakeGitHub()
    github.login = "manager"
    monkeypatch.setattr(host, "GitHub", lambda: github)
    controller = GitHubWorkflow(vm)
    controller.add("owner/project", False, None)
    upload, config, registration = vm.ssh.call_args_list
    assert "tar -xf" in upload.args[0]
    assert isinstance(upload.kwargs["input"], bytes)
    assert config.args[0].endswith(" config") and config.kwargs["capture"]
    assert registration.args[0].endswith(" add owner/project")


def test_unprotected_branch_reports_setup_instead_of_token_error(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue = environment
    items = github.items
    monkeypatch.setattr(github, "items", lambda path: [{"name": "master", "protected": False}] if path.endswith("/branches") else items(path))
    with pytest.raises(RuntimeError, match="master: GitHub reports this branch is unprotected") as error:
        host.check_repository(github, "owner/project", "manager")
    assert "https://github.com/owner/project/settings/branches" in str(error.value)


def test_host_protection_404_explains_permissions_and_classic_rules(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue = environment
    original_api = github.api

    def api(path: str, *args: Any, **kwargs: Any) -> Any:
        if path.endswith("/protection"):
            raise RuntimeError("gh: Not Found (HTTP 404)")
        return original_api(path, *args, **kwargs)

    monkeypatch.setattr(github, "api", api)
    with pytest.raises(RuntimeError, match="cannot read classic branch protection") as error:
        host.check_repository(github, "owner/project", "manager")
    message = str(error.value)
    assert "rule is absent or the credential cannot read it" in message
    assert "ruleset-only protection is not supported" in message
    assert "Authenticate a token on the host" in message
    assert "protection-login" not in message


@pytest.mark.parametrize("unsafe, login", [(True, "manager"), (False, "developer")])
def test_host_add_blocks_registration_when_policy_or_identity_invalid(monkeypatch: pytest.MonkeyPatch, unsafe: bool, login: str) -> None:
    vm = Mock()
    vm.ssh.return_value = subprocess.CompletedProcess([], 0, json.dumps(CONFIG), "")
    github = FakeGitHub()
    github.login = login
    if unsafe:
        github.protection["required_pull_request_reviews"]["require_code_owner_reviews"] = False
    monkeypatch.setattr(host, "GitHub", lambda: github)
    with pytest.raises(RuntimeError):
        GitHubWorkflow(vm).add("owner/project", False, None)
    assert all(not call.args[0].endswith(" add owner/project") for call in vm.ssh.call_args_list)
    assert not github.posts


def test_host_check_uses_manager_credentials_without_guest_policy_api(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    vm = Mock()
    vm.ssh.return_value = subprocess.CompletedProcess([], 0, json.dumps(CONFIG), "")
    github = FakeGitHub()
    github.login = "manager"
    monkeypatch.setattr(host, "GitHub", lambda: github)
    GitHubWorkflow(vm).action("check")
    assert "full protection policy verified on the host" in capsys.readouterr().out
    assert len(vm.ssh.call_args_list) == 2  # Upload and read non-secret guest config.
    github.protection["required_pull_request_reviews"]["required_approving_review_count"] = 0
    with pytest.raises(RuntimeError, match="approving review"):
        GitHubWorkflow(vm).action("check")


def test_guest_only_observes_protected_flag_after_host_verification(environment: Any) -> None:
    github, worker, queue = environment
    github.protection["required_pull_request_reviews"]["required_approving_review_count"] = 0
    worker.poll()
    assert not worker.errors and queue.next() is not None
    with pytest.raises(RuntimeError, match="approving review"):
        host.check_repository(github, "owner/project", "manager")


def test_guest_removes_retired_policy_token_without_exposing_it(environment: Any, capsys: pytest.CaptureFixture[str]) -> None:
    legacy = workflow.CONFIG.parent / "github-protection-token"
    legacy.write_text("retired-secret")
    workflow.main(["config"])
    assert not legacy.exists()
    output = capsys.readouterr().out
    assert json.loads(output) == CONFIG
    assert "retired-secret" not in output


@pytest.mark.parametrize("protected", [False, None])
def test_guest_fails_closed_when_branch_protection_unknown(environment: Any, monkeypatch: pytest.MonkeyPatch, protected: bool | None) -> None:
    github, worker, queue = environment
    original_items = github.items
    monkeypatch.setattr(github, "items", lambda path: [{"name": "main", "protected": protected}] if path.endswith("/branches") else original_items(path))
    worker.poll()
    assert worker.errors and queue.next() is None


def test_poll_logs_results_and_unchanged_work_without_issue_content(environment: Any, caplog: pytest.LogCaptureFixture) -> None:
    github, worker, queue = environment
    github.issue["body"] = "private-task-content-must-not-be-logged"
    with caplog.at_level("INFO", logger=workflow.LOGGER.name):
        worker.poll()
        worker.poll()
    messages = caplog.text
    assert "Poll started: developer=@developer repositories=1" in messages
    assert "found 1 open issues assigned to @developer" in messages
    assert "Queued job=1 issue=owner/project#1" in messages
    assert "new_or_updated=1 unchanged=0" in messages
    assert "new_or_updated=0 unchanged=1" in messages
    assert "queued=1 waiting=0 review=0 failed=0" in messages
    assert "private-task-content" not in messages


def test_poll_and_job_failures_are_logged(environment: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    github, worker, queue = environment
    with caplog.at_level("INFO", logger=workflow.LOGGER.name):
        worker.poll()
        monkeypatch.setattr(worker, "execute", Mock(side_effect=RuntimeError("Codex unavailable")))
        worker.run(once=True)
    assert "GitHub workflow started" in caplog.text
    assert "state queued -> running" in caplog.text
    assert "state running -> failed: Codex unavailable" in caplog.text
    assert "Finished job=1" in caplog.text
    assert "GitHub workflow stopped" in caplog.text
    github.protected = False
    caplog.clear()
    with caplog.at_level("INFO", logger=workflow.LOGGER.name):
        worker.poll()
    assert "poll failed; work blocked" in caplog.text


def test_codex_publication_steps_are_logged(environment: Any, local_remote: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    github, worker, queue = environment

    def codex(path: Path, *args: Any) -> dict[str, str]:
        (path / "feature.txt").write_text("Feature")
        return {"status": "ready", "title": "Private title", "message": "Private result"}

    monkeypatch.setattr(workflow, "run_codex", codex)
    with caplog.at_level("INFO", logger=workflow.LOGGER.name):
        worker.poll()
        worker.execute(queue.next())
    for step in (
        "cloning repository",
        "synchronizing branch",
        "starting Codex; output=",
        "Codex finished: result=ready",
        "before publication",
        "committing implementation",
        "pushing branch",
        "creating pull request",
        "requesting review",
        "-> review",
    ):
        assert step in caplog.text
    assert "Private title" not in caplog.text and "Private result" not in caplog.text


def test_logging_is_immediate_private_and_rotates(environment: Any, capsys: pytest.CaptureFixture[str]) -> None:
    from logging.handlers import RotatingFileHandler

    logger = workflow.LOGGER
    previous_handlers, previous_level, previous_propagate = logger.handlers[:], logger.level, logger.propagate
    logger.handlers = []
    try:
        workflow.configure_logging()
        logger.info("Poll completed: queued=3")
        path = workflow.STATE / "workflow.log"
        assert "Poll completed: queued=3" in path.read_text()
        assert "Poll completed: queued=3" in capsys.readouterr().err
        assert path.stat().st_mode & 0o777 == 0o600
        handler = next(handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler))
        assert handler.maxBytes == 5 * 1024 * 1024 and handler.backupCount == 3
        handler.maxBytes = 1
        logger.info("Next poll")
        assert path.with_name("workflow.log.1").exists()
    finally:
        for closed_handler in logger.handlers:
            closed_handler.close()
        logger.handlers = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def test_poll_logs_registered_scope_and_warns_when_no_repositories(environment: Any, caplog: pytest.LogCaptureFixture) -> None:
    github, worker, queue = environment
    with caplog.at_level("INFO", logger=workflow.LOGGER.name):
        worker.poll()
    assert "Registered repositories being polled: owner/project" in caplog.text
    assert workflow.status()["repositories"] == ["owner/project"]
    assert workflow.status()["developer"] == "developer"
    worker.config["repositories"] = []
    caplog.clear()
    with caplog.at_level("INFO", logger=workflow.LOGGER.name):
        worker.poll()
    assert "Registered repositories being polled: none" in caplog.text
    assert "Adding a GitHub collaborator does not register a project" in caplog.text
    assert "github add OWNER/REPO" in caplog.text


@pytest.mark.parametrize(
    "stage, expected",
    [
        ("repository", "Host token cannot read repository owner/project"),
        ("permission", "Host could not verify @manager's access"),
        ("branches", "the host token needs Contents read permission"),
    ],
)
def test_host_404_reports_the_failed_setup_stage(environment: Any, monkeypatch: pytest.MonkeyPatch, stage: str, expected: str) -> None:
    github, worker, queue = environment
    if stage == "branches":
        monkeypatch.setattr(github, "items", Mock(side_effect=RuntimeError("HTTP 404")))
    else:
        original_api = github.api

        def api(path: str, *args: Any, **kwargs: Any) -> Any:
            if (stage == "repository" and path == "repos/owner/project") or (stage == "permission" and path.endswith("/permission")):
                raise RuntimeError("HTTP 404")
            return original_api(path, *args, **kwargs)

        monkeypatch.setattr(github, "api", api)
    with pytest.raises(RuntimeError) as error:
        host.check_repository(github, "owner/project", "manager")
    assert expected in str(error.value)
    assert "HTTP 404" in str(error.value)


@pytest.mark.parametrize("enforce_admins", [True, False, None])
def test_protection_allows_administrator_bypass(enforce_admins: bool | None) -> None:
    protection = copy.deepcopy(PROTECTION)
    protection["enforce_admins"] = {"enabled": enforce_admins} if enforce_admins is not None else None
    assert host.protection_problems(protection, "* @manager", "manager") == []


def test_pending_review_does_not_queue_work_but_submission_does(environment: Any, caplog: pytest.LogCaptureFixture) -> None:
    github, worker, queue = environment
    github.pull = {
        "number": 2,
        "state": "open",
        "merged_at": None,
        "user": {"login": "developer"},
        "head": {"ref": "agent-worker/issue-1", "repo": {"full_name": "owner/project"}},
    }
    with caplog.at_level("INFO", logger=workflow.LOGGER.name):
        worker.poll()
    original = queue.next()
    queue.update(original, "review")
    assert "inline_comments=0 submitted_reviews=0" in caplog.text
    assert "pending review comments are visible only to their author" in caplog.text
    github.reviews.append({"id": 70, "state": "PENDING", "body": "", "user": {"login": "manager"}})
    worker.poll()
    assert queue.next() is None
    github.reviews[0]["state"] = "COMMENTED"
    github.inline.append({"id": 71, "body": "Change this line", "path": "hello.py", "line": 1, "user": {"login": "manager"}})
    caplog.clear()
    with caplog.at_level("INFO", logger=workflow.LOGGER.name):
        worker.poll()
    assert queue.next() is not None
    assert "inline_comments=1 submitted_reviews=1" in caplog.text
    assert "manager=@manager inline_comments=1" in caplog.text


def test_rate_limit_aborts_poll_and_keeps_jobs_queued(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue = environment
    worker.poll()
    job = queue.next()
    execute = Mock()
    monkeypatch.setattr(worker, "execute", execute)
    github.retry_at = 2000000000
    monkeypatch.setattr(github, "api", Mock(side_effect=workflow.RateLimitError("rate limited")))
    worker.run(once=True)
    execute.assert_not_called()
    assert queue.next()["id"] == job["id"]
    assert workflow.status()["rate_limited_until"] == 2000000000


def test_rate_limit_during_publication_defers_instead_of_failing_job(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue = environment
    worker.poll()
    monkeypatch.setattr(worker, "execute", Mock(side_effect=workflow.RateLimitError("rate limited")))
    worker.run(once=True)
    assert queue.summary()[0]["state"] == "queued"
    assert "rate limited" in worker.errors["poll"]


def test_manual_poll_is_coalesced_without_starting_work(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue = environment
    worker.publish_status()
    github.api = Mock(side_effect=AssertionError("Manual request must not call GitHub"))
    assert workflow.request_poll() == "Poll requested"
    assert workflow.request_poll() == "Poll requested"
    assert (workflow.STATE / "poll-request").stat().st_mode & 0o777 == 0o600
    worker.wait_for_poll(60)  # Pending request skips the normal wait immediately.
    assert not (workflow.STATE / "poll-request").exists()
    assert not worker.consume_poll_request()
    github.api.assert_not_called()
    assert queue.summary() == []


def test_manual_poll_waits_for_active_task_and_rate_cooldown(environment: Any) -> None:
    import time

    github, worker, queue = environment
    worker.active = "owner/project#1"
    worker.publish_status()
    assert workflow.request_poll() == "Poll queued after the current task"
    assert worker.active == "owner/project#1"
    github.retry_at = time.time() + 120
    worker.publish_status()
    assert workflow.request_poll() == "Poll queued until rate-limit cooldown ends"
    assert not worker.consume_poll_request()
    assert (workflow.STATE / "poll-request").exists()
    worker.active = None
    github.retry_at = 0
    assert worker.consume_poll_request()


@pytest.mark.parametrize(
    "state, supported, message",
    [
        ("stopped", True, "Start the GitHub worker first"),
        ("unavailable", True, "Start the GitHub worker first"),
        ("disabled", True, "Start the GitHub worker first"),
        ("idle", False, "Restart the GitHub worker"),
    ],
)
def test_manual_poll_rejects_inactive_or_old_workers(environment: Any, state: str, supported: bool, message: str) -> None:
    import time

    workflow.atomic_json(workflow.STATE / "status.json", {"state": state, "manual_poll_supported": supported, "heartbeat": time.time()})
    with pytest.raises(RuntimeError, match=message):
        workflow.request_poll()
    assert not (workflow.STATE / "poll-request").exists()


def test_manual_poll_wakes_an_idle_worker(environment: Any) -> None:
    import threading

    github, worker, queue = environment
    worker.publish_status()
    done = threading.Event()

    def wait() -> None:
        worker.wait_for_poll(60)
        done.set()

    thread = threading.Thread(target=wait)
    thread.start()
    try:
        workflow.request_poll()
        assert done.wait(3), "Local poll request did not wake the worker"
    finally:
        worker.stop.set()
        thread.join(timeout=3)
    assert not (workflow.STATE / "poll-request").exists()


@pytest.mark.parametrize(
    "action, remote",
    [
        ("restart-service", "systemctl --user restart agent-worker-github.service"),
        ("log", "journalctl --user -u agent-worker-github.service -f"),
    ],
)
def test_service_shortcuts_dispatch_directly_to_ssh(action: str, remote: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vm = Mock()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agent_worker.main.VM", lambda settings: vm)
    assert Application().start(["github", action]) == 0
    vm.ssh.assert_called_once_with(remote)
