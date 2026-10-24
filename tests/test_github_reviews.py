from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import Mock

import pytest

from agent_worker.guest import github_review as review
from agent_worker.guest import github_workflow as workflow
from test_github_workflow import CONFIG, FakeGitHub, git, local_remote  # noqa: F401


class ReviewGitHub(FakeGitHub):
    def __init__(self, head: str, base: str) -> None:
        super().__init__()
        self.issue["assignees"] = []
        self.request: dict[str, Any] = {
            "number": 2,
            "state": "open",
            "draft": False,
            "title": "Human feature",
            "body": "Review my code",
            "html_url": "https://github.com/owner/project/pull/2",
            "user": {"login": "manager"},
            "requested_reviewers": [{"login": "developer"}],
            "head": {"sha": head, "repo": {"full_name": "contributor/fork"}},
            "base": {"sha": base},
        }
        self.events = [{"id": 10, "event": "review_requested", "requested_reviewer": {"login": "developer"}}]
        self.published: list[dict[str, Any]] = []
        self.files = [{"filename": "hello.py", "patch": '@@ -0,0 +1 @@\n+print("Hello")'}]

    def items(self, path: str) -> list[dict[str, Any]]:
        if path == "repos/owner/project/pulls?state=open":
            return [copy.deepcopy(self.request)] if self.request["state"] == "open" else []
        if path.endswith("/issues/2/timeline"):
            return copy.deepcopy(self.events)
        if path.endswith("/pulls/2/reviews"):
            return copy.deepcopy(self.published)
        if path.endswith("/pulls/2/files"):
            return copy.deepcopy(self.files)
        return super().items(path)

    def api(self, path: str, method: str = "GET", body: Any = None, pages: bool = False) -> Any:
        if path == "repos/owner/project/pulls/2" and method == "GET":
            return copy.deepcopy(self.request)
        if path == "repos/owner/project/pulls/2/reviews" and method == "POST":
            self.posts.append((path, copy.deepcopy(body)))
            self.published.append(dict(body, id=len(self.published) + 100, user={"login": "developer"}, state="COMMENTED"))
            return self.published[-1]
        return super().api(path, method, body, pages)


@pytest.fixture
def review_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_remote: Path
) -> Iterator[tuple[ReviewGitHub, workflow.Worker, workflow.Queue, Path]]:
    monkeypatch.setattr(workflow, "STATE", tmp_path / "state")
    monkeypatch.setattr(workflow, "REPOSITORIES", tmp_path / "checkouts")
    human = tmp_path / "human"
    git("clone", str(local_remote), str(human))
    git("config", "user.name", "Manager", cwd=human)
    git("config", "user.email", "manager@example.com", cwd=human)
    git("checkout", "-b", "human/feature", cwd=human)
    (human / "hello.py").write_text('print("Hello")\n')
    git("add", ".", cwd=human)
    git("commit", "-m", "Human feature", cwd=human)
    git("push", "origin", "HEAD:refs/pull/2/head", cwd=human)
    github = ReviewGitHub(git("rev-parse", "HEAD", cwd=human), git("rev-parse", "main", cwd=local_remote))
    queue = workflow.Queue(tmp_path / "state/queue.sqlite3")
    worker = workflow.Worker(copy.deepcopy(CONFIG), github, queue)
    yield github, worker, queue, human
    queue.db.close()


def result() -> dict[str, Any]:
    return {
        "summary": "Reviewed the feature; found a bug.",
        "findings": [{"path": "hello.py", "line": 1, "side": "RIGHT", "body": "[P2] Explain the concrete defect"}],
    }


def test_requested_review_publishes_inline_findings_without_issue_assignment_or_push(review_environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue, human = review_environment
    calls: list[tuple[str, ...]] = []
    command = workflow.command

    def record(*args: str, **kwargs: Any) -> str:
        calls.append(args)
        return command(*args, **kwargs)

    monkeypatch.setattr(workflow, "command", record)
    codex = Mock(return_value=result())
    monkeypatch.setattr(review, "run_review", codex)
    worker.poll()
    job = queue.next()
    assert job["kind"] == "review" and job["issue"] == 2
    worker.execute_review(job)
    assert queue.summary()[0]["state"] == "reviewed"
    assert len(github.published) == 1
    assert github.published[0]["commit_id"] == github.request["head"]["sha"]
    assert github.published[0]["comments"] == result()["findings"]
    assert "Reviewed commit" in github.published[0]["body"]
    assert not any(args[:2] in (("git", "push"), ("git", "commit")) for args in calls)
    assert git("rev-parse", "HEAD", cwd=human) == github.request["head"]["sha"]
    # COMMENT reviews may leave the reviewer requested; do not repeat automatically.
    worker.poll()
    assert queue.next() is None
    codex.assert_called_once()


def test_explicit_rerequest_of_same_commit_creates_another_review(review_environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue, human = review_environment
    codex = Mock(return_value=result())
    monkeypatch.setattr(review, "run_review", codex)
    worker.poll()
    worker.execute_review(queue.next())
    github.events.append({"id": 11, "event": "review_requested", "requested_reviewer": {"login": "developer"}})
    worker.poll()
    worker.execute_review(queue.next())
    assert len(github.published) == 2 and codex.call_count == 2
    assert github.published[0]["commit_id"] == github.published[1]["commit_id"]


def test_changed_head_supersedes_review_without_publication(review_environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue, human = review_environment
    old = github.request["head"]["sha"]

    def codex(*args: Any) -> dict[str, Any]:
        (human / "hello.py").write_text('print("Updated")\n')
        git("add", ".", cwd=human)
        git("commit", "-m", "Update feature", cwd=human)
        git("push", "origin", "HEAD:refs/pull/2/head", cwd=human)
        github.request["head"]["sha"] = git("rev-parse", "HEAD", cwd=human)
        return result()

    monkeypatch.setattr(review, "run_review", codex)
    worker.poll()
    first = queue.next()
    worker.execute_review(first)
    assert not github.published
    assert queue.db.execute("SELECT state FROM jobs WHERE id=?", (first["id"],)).fetchone()[0] == "superseded"
    assert json.loads(queue.next()["context"])["head_sha"] != old


@pytest.mark.parametrize("withdraw", [True, False])
def test_withdrawn_request_or_closed_pr_prevents_publication(review_environment: Any, monkeypatch: pytest.MonkeyPatch, withdraw: bool) -> None:
    github, worker, queue, human = review_environment

    def codex(*args: Any) -> dict[str, Any]:
        if withdraw:
            github.request["requested_reviewers"] = []
        else:
            github.request["state"] = "closed"
        return result()

    monkeypatch.setattr(review, "run_review", codex)
    worker.poll()
    worker.execute_review(queue.next())
    assert not github.published
    assert queue.summary()[0]["state"] == "cancelled"


def test_lost_review_response_is_idempotent_on_retry(review_environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue, human = review_environment
    codex = Mock(return_value=result())
    monkeypatch.setattr(review, "run_review", codex)
    api = github.api

    def lose_response(path: str, method: str = "GET", *args: Any, **kwargs: Any) -> Any:
        response = api(path, method, *args, **kwargs)
        if method == "POST" and path.endswith("/reviews"):
            github.request["requested_reviewers"] = []
            raise RuntimeError("Connection lost after publication")
        return response

    monkeypatch.setattr(github, "api", lose_response)
    worker.poll()
    job = queue.next()
    with pytest.raises(RuntimeError, match="Connection lost"):
        worker.execute_review(job)
    queue.update(job, "failed")
    monkeypatch.setattr(github, "api", api)
    worker.execute_review(job)
    assert len(github.published) == 1
    assert queue.summary()[0]["state"] == "reviewed"
    codex.assert_called_once()


def test_missing_request_does_not_cancel_review_as_an_unassigned_issue(review_environment: Any) -> None:
    github, worker, queue, human = review_environment
    worker.poll()
    worker.poll()
    assert queue.next()["kind"] == "review"
    github.request["requested_reviewers"] = []
    worker.poll()
    assert queue.summary()[0]["state"] == "cancelled"


@pytest.mark.parametrize("reason", ["draft", "self", "not_requested"])
def test_review_eligibility_ignores_drafts_self_and_unrequested_prs(review_environment: Any, reason: str) -> None:
    github, worker, queue, human = review_environment
    if reason == "draft":
        github.request["draft"] = True
    elif reason == "self":
        github.request["user"]["login"] = "developer"
    else:
        github.request["requested_reviewers"] = [{"login": "someone_else"}]
    worker.poll()
    assert queue.next() is None


def test_worker_dispatches_review_task_kind(review_environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue, human = review_environment
    implementation = Mock()
    reviewer = Mock(side_effect=lambda job: queue.update(job, "reviewed"))
    monkeypatch.setattr(worker, "execute", implementation)
    monkeypatch.setattr(worker, "execute_review", reviewer)
    worker.run(once=True)
    reviewer.assert_called_once()
    implementation.assert_not_called()
    assert queue.summary()[0]["state"] == "reviewed"


def test_review_request_survives_queue_restart(review_environment: Any) -> None:
    github, worker, queue, human = review_environment
    worker.poll()
    other = workflow.Queue(workflow.STATE / "queue.sqlite3")
    try:
        job = other.next()
        assert job is not None and job["kind"] == "review"
        assert json.loads(job["context"])["request_id"] == "10"
    finally:
        other.db.close()


def test_old_queue_schema_migrates_without_losing_jobs(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE jobs(id INTEGER PRIMARY KEY, repo TEXT, issue INTEGER, fingerprint TEXT, context TEXT, state TEXT, error TEXT DEFAULT '', UNIQUE(repo,issue,fingerprint))"
    )
    db.execute("INSERT INTO jobs VALUES(1,'owner/project',1,'original','{}','queued','')")
    db.commit()
    db.close()
    queue = workflow.Queue(path)
    try:
        job = queue.next()
        assert job is not None and job["id"] == 1 and job["kind"] == "issue"
    finally:
        queue.db.close()


def test_inline_findings_use_changed_lines_and_preserve_unanchored_findings() -> None:
    review_result: dict[str, Any] = {
        "summary": "Summary",
        "findings": [
            {"path": "hello.py", "line": 1, "side": "LEFT", "body": "Deleted-line finding"},
            {"path": "hello.py", "line": 2, "side": "RIGHT", "body": "Added-line finding"},
            {"path": "hello.py", "line": 999, "side": "RIGHT", "body": "Cannot anchor this"},
        ],
    }
    review_result["findings"].append(review_result["findings"][1].copy())
    summary, comments = review.publication(review_result, [{"filename": "hello.py", "patch": "@@ -1 +1,2 @@\n-old\n+new\n+more"}])
    assert len(comments) == 2
    assert "Cannot anchor this" in summary and "could not anchor inline" in summary


def test_codex_review_runs_read_only_and_returns_structured_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def execute(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[:6] == ["codex", "--sandbox", "read-only", "--ask-for-approval", "never", "exec"]
        assert "Do not edit files" in kwargs["input"]
        assert "git diff BASE_SHA...HEAD_SHA" in kwargs["input"]
        Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(result()))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(review.subprocess, "run", execute)
    assert review.run_review(tmp_path, {"head_sha": "a" * 40, "base_sha": "b" * 40}, tmp_path / "run")["findings"]


def test_base_movement_alone_does_not_repeat_a_completed_review(review_environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    github, worker, queue, human = review_environment
    codex = Mock(return_value=result())
    monkeypatch.setattr(review, "run_review", codex)
    worker.poll()
    worker.execute_review(queue.next())
    github.request["base"]["sha"] = "c" * 40
    worker.poll()
    assert queue.next() is None
    assert len(github.published) == 1
    codex.assert_called_once()


@pytest.mark.parametrize(
    "invalid", [None, [], {}, {"summary": "Summary", "findings": [{"path": "hello.py", "line": True, "side": "RIGHT", "body": "Finding"}]}]
)
def test_review_output_validation_rejects_malformed_model_results(invalid: Any) -> None:
    with pytest.raises(ValueError, match="invalid review"):
        review.validate_result(invalid)
