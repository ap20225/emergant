"""Iteration 6 isolated contract tests for owner connection/publication/worker pathways."""

# Modules/features covered: GitHub connect verify contracts, encrypted vault storage, publication one-shot flow,
# runner remote-only sequencing, and worker WAITING_APPROVAL contract via mocks only.
import asyncio
import json
import os
import sys
import types

import pytest
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

sys.path.append("/app/backend")
from bounty.connections import Connections, GitHubConnectionInput
from bounty.live_worker import LiveWorker
from bounty.publication import Publisher
from bounty.runner import RemoteRunner


def _run(coro):
    return asyncio.run(coro)


class _Collection:
    def __init__(self):
        self.docs = {}
        self.calls = []

    async def update_one(self, query, update, upsert=False):
        self.calls.append((query, update, upsert))
        key = query.get("id") if isinstance(query, dict) else None
        if key:
            current = self.docs.get(key, {})
            current.update(update.get("$set", {}))
            self.docs[key] = current

    async def delete_one(self, query):
        self.calls.append(("delete", query))
        key = query.get("id") if isinstance(query, dict) else None
        if key in self.docs:
            del self.docs[key]

    async def count_documents(self, query):
        if "created_at" in query:
            return len(self.docs)
        return len(self.docs)


class _DB:
    def __init__(self):
        self.integration_connections = _Collection()
        self.jobs = _Collection()
        self.pull_requests = _Collection()
        self.publication_attempts = _Collection()


class _Store:
    def __init__(self):
        self.db = _DB()
        self._rows = {"integration_connections": {}, "jobs": {}, "pull_requests": {}, "execution_attestations": {}}
        self.audits = []
        self.inserted = []

    async def controls(self):
        return {
            "global_stop": False,
            "agents_stopped": False,
            "submissions_disabled": False,
            "integrations_revoked": False,
            "spending_stopped": False,
        }

    async def settings(self):
        return {
            "mode": "ASSISTED",
            "max_submissions_per_day": 10,
            "max_execution_time": 30,
            "max_concurrent_jobs": 2,
            "max_jobs_per_day": 10,
        }

    async def one(self, collection, query):
        if collection in self._rows and isinstance(query, dict) and "id" in query:
            return self._rows[collection].get(query["id"])
        if collection == "pull_requests" and "job_id" in query:
            for row in self._rows["pull_requests"].values():
                if row.get("job_id") == query["job_id"]:
                    return row
        return None

    async def rows(self, collection, limit=None):
        rows = list(self._rows.get(collection, {}).values())
        return rows[:limit] if limit else rows

    async def insert(self, collection, doc):
        if collection == "publication_attempts" and doc["id"] in self._rows["execution_attestations"]:
            raise DuplicateKeyError("duplicate")
        if collection == "publication_attempts":
            self._rows["execution_attestations"][doc["id"]] = doc
        saved = dict(doc)
        saved.setdefault("id", saved.get("job_id", f"{collection}_1"))
        self.inserted.append((collection, saved))
        if collection in self._rows:
            self._rows[collection][saved["id"]] = saved
        return saved

    async def audit(self, *args, **kwargs):
        self.audits.append({"args": args, "kwargs": kwargs})


def _valid_connection_input():
    return GitHubConnectionInput(token="ghp_abcdefghijklmnopqrstuvwxyz", repository="owner/repo", issue_number=7)


def test_connect_github_get_only_and_encrypted_storage(monkeypatch, tmp_path):
    os.environ["PENNILESS_SECRETS_DIR"] = str(tmp_path)
    store = _Store()
    c = Connections(store)
    calls = []

    async def fake_get(path, token):
        calls.append(path)
        if path == "/user":
            return {"login": "owner"}
        if path == "/repos/owner/repo":
            return {"full_name": "owner/repo", "private": False, "archived": False, "disabled": False, "owner": {"login": "owner"}, "default_branch": "main", "size": 12}
        if path == "/repos/owner/repo/issues/7":
            return {"title": "Issue 7", "state": "open"}
        raise AssertionError("Unexpected path")

    monkeypatch.setattr(c, "github_get", fake_get)
    out = _run(c.connect_github(_valid_connection_input(), "owner-user"))

    assert calls == ["/user", "/repos/owner/repo", "/repos/owner/repo/issues/7"]
    assert out["status"] == "CONNECTED"
    assert "token" not in json.dumps(out)
    encrypted = (tmp_path / "github.enc").read_bytes()
    assert b"ghp_abcdefghijklmnopqrstuvwxyz" not in encrypted
    assert all("ghp_abcdefghijklmnopqrstuvwxyz" not in str(a) for a in store.audits)


def test_connect_github_rejects_owner_mismatch(monkeypatch, tmp_path):
    os.environ["PENNILESS_SECRETS_DIR"] = str(tmp_path)
    store = _Store()
    c = Connections(store)

    async def fake_get(path, token):
        if path == "/user":
            return {"login": "alice"}
        if path == "/repos/owner/repo":
            return {"full_name": "owner/repo", "private": False, "archived": False, "disabled": False, "owner": {"login": "owner"}, "default_branch": "main"}
        return {"title": "Issue 7", "state": "open"}

    monkeypatch.setattr(c, "github_get", fake_get)
    with pytest.raises(HTTPException) as exc:
        _run(c.connect_github(_valid_connection_input(), "owner-user"))
    assert exc.value.status_code == 403
    assert "owned by the authenticated GitHub user" in exc.value.detail


def test_connect_github_rejects_private_and_pull_request(monkeypatch, tmp_path):
    os.environ["PENNILESS_SECRETS_DIR"] = str(tmp_path)
    store = _Store()
    c = Connections(store)

    async def private_repo(path, token):
        if path == "/user":
            return {"login": "owner"}
        if path == "/repos/owner/repo":
            return {"full_name": "owner/repo", "private": True, "archived": False, "disabled": False, "owner": {"login": "owner"}, "default_branch": "main"}
        return {"title": "Issue 7", "state": "open"}

    monkeypatch.setattr(c, "github_get", private_repo)
    with pytest.raises(HTTPException) as private_exc:
        _run(c.connect_github(_valid_connection_input(), "owner-user"))
    assert private_exc.value.status_code == 403

    async def pull_req(path, token):
        if path == "/user":
            return {"login": "owner"}
        if path == "/repos/owner/repo":
            return {"full_name": "owner/repo", "private": False, "archived": False, "disabled": False, "owner": {"login": "owner"}, "default_branch": "main"}
        return {"title": "PR", "state": "open", "pull_request": {"url": "x"}}

    monkeypatch.setattr(c, "github_get", pull_req)
    with pytest.raises(HTTPException) as pr_exc:
        _run(c.connect_github(_valid_connection_input(), "owner-user"))
    assert pr_exc.value.status_code == 422


def test_connect_github_single_repo_lock_conflict(monkeypatch, tmp_path):
    os.environ["PENNILESS_SECRETS_DIR"] = str(tmp_path)
    store = _Store()
    store._rows["integration_connections"]["github"] = {"id": "github", "repository": "other/repo"}
    c = Connections(store)

    with pytest.raises(HTTPException) as exc:
        _run(c.connect_github(_valid_connection_input(), "owner-user"))
    assert exc.value.status_code == 409
    assert "Disconnect the existing repository" in exc.value.detail


class _GitHub:
    def __init__(self):
        self.calls = []

    async def request(self, method, path, private=False):
        self.calls.append((method, path, private))
        if method == "GET" and "/git/ref/heads/" in path:
            return {"object": {"sha": "base-sha"}}
        if method == "GET" and "/git/commits/" in path:
            return {"tree": {"sha": "tree-base"}}
        raise AssertionError(f"Unexpected request {method} {path}")


def _approved_job():
    return {
        "id": "job_1",
        "repository": "owner/repo",
        "simulated": False,
        "status": "APPROVED",
        "approval_status": "APPROVED",
        "test_status": "PASS",
        "security_status": "PASS",
        "review_status": "APPROVE",
        "base_sha": "base-sha",
        "base_branch": "main",
        "branch": "penniless/job_1",
        "patch_files": {"src/a.py": "print('ok')\n"},
        "artifact_hash": "",
        "approved_artifact_hash": "",
        "pr_id": "pr_1",
    }


def test_publisher_gate_rejects_simulated_and_hash_mismatch(monkeypatch, tmp_path):
    os.environ["PENNILESS_SECRETS_DIR"] = str(tmp_path)
    store = _Store()
    gh = _GitHub()
    pub = Publisher(store, gh)

    job = _approved_job()
    job["simulated"] = True
    store._rows["jobs"]["job_1"] = job
    store._rows["integration_connections"]["github"] = {"id": "github", "status": "CONNECTED", "repository": "owner/repo"}
    from bounty.secrets import SecretVault
    SecretVault().put("github", "ghp_abcdefghijklmnopqrstuvwxyz")

    with pytest.raises(HTTPException) as sim_exc:
        _run(pub.gate(job))
    assert sim_exc.value.status_code == 409

    job2 = _approved_job()
    job2["artifact_hash"] = "a"
    job2["approved_artifact_hash"] = "b"
    with pytest.raises(HTTPException) as hash_exc:
        _run(pub.gate(job2))
    assert hash_exc.value.status_code == 409
    assert "Artifact changed" in hash_exc.value.detail


def test_publisher_publish_one_shot_and_draft_only(monkeypatch, tmp_path):
    os.environ["PENNILESS_SECRETS_DIR"] = str(tmp_path)
    store = _Store()
    gh = _GitHub()
    pub = Publisher(store, gh)
    from bounty.live_worker import artifact_hash
    from bounty.secrets import SecretVault

    job = _approved_job()
    digest = artifact_hash(job["patch_files"])
    job["artifact_hash"] = digest
    job["approved_artifact_hash"] = digest
    store._rows["jobs"]["job_1"] = job
    store._rows["pull_requests"]["pr_1"] = {"id": "pr_1", "title": "Fix", "body": "Body"}
    store._rows["integration_connections"]["github"] = {"id": "github", "status": "CONNECTED", "repository": "owner/repo"}
    SecretVault().put("github", "ghp_abcdefghijklmnopqrstuvwxyz")

    writes = []

    async def fake_write(_job, path, payload):
        writes.append((path, payload))
        if path.endswith("/git/trees"):
            return {"sha": "tree-1"}
        if path.endswith("/git/commits"):
            return {"sha": "commit-1"}
        if path.endswith("/git/refs"):
            return {"ref": payload["ref"]}
        if path.endswith("/pulls"):
            return {"draft": True, "number": 42, "html_url": "https://github.com/owner/repo/pull/42"}
        raise AssertionError("Unexpected write path")

    monkeypatch.setattr(pub, "write", fake_write)

    result = _run(pub.publish("job_1", "owner-user"))
    assert result["external"] is True
    assert any(path.endswith("/git/trees") for path, _ in writes)
    assert any(path.endswith("/git/commits") for path, _ in writes)
    assert any(path.endswith("/git/refs") for path, _ in writes)
    pull = [p for p in writes if p[0].endswith("/pulls")][0]
    assert pull[1]["draft"] is True
    assert all("merge" not in p[0] and "force" not in json.dumps(p[1]).lower() for p in writes)

    with pytest.raises(HTTPException) as dup_exc:
        _run(pub.publish("job_1", "owner-user"))
    assert dup_exc.value.status_code == 409
    assert "already attempted" in dup_exc.value.detail


def test_publisher_rejects_base_change(monkeypatch, tmp_path):
    os.environ["PENNILESS_SECRETS_DIR"] = str(tmp_path)
    store = _Store()

    class _GitHubBaseMismatch(_GitHub):
        async def request(self, method, path, private=False):
            if method == "GET" and "/git/ref/heads/" in path:
                return {"object": {"sha": "new-sha"}}
            return await super().request(method, path, private)

    gh = _GitHubBaseMismatch()
    pub = Publisher(store, gh)
    from bounty.live_worker import artifact_hash
    from bounty.secrets import SecretVault

    job = _approved_job()
    digest = artifact_hash(job["patch_files"])
    job["artifact_hash"] = digest
    job["approved_artifact_hash"] = digest
    store._rows["jobs"]["job_1"] = job
    store._rows["pull_requests"]["pr_1"] = {"id": "pr_1", "title": "Fix", "body": "Body"}
    store._rows["integration_connections"]["github"] = {"id": "github", "status": "CONNECTED", "repository": "owner/repo"}
    SecretVault().put("github", "ghp_abcdefghijklmnopqrstuvwxyz")

    with pytest.raises(HTTPException) as exc:
        _run(pub.publish("job_1", "owner-user"))
    assert exc.value.status_code == 409
    assert "Base branch changed" in exc.value.detail


def test_remote_runner_create_sequence_disables_network_before_checkout(monkeypatch):
    os.environ["BOUNTY_REMOTE_WORKDIR"] = "/work"
    os.environ["BOUNTY_REMOTE_HOME"] = "/home/user"
    os.environ["RUNNER_HARD_TIMEOUT_SECONDS"] = "60"
    os.environ["GITHUB_WEB_URL"] = "https://github.com"
    os.environ["E2B_API_URL"] = "https://e2b.example"

    class _StoreRunner(_Store):
        async def one(self, collection, query):
            if collection == "integration_connections" and query == {"id": "runner"}:
                return {"id": "runner", "template_id": "tmpl_1"}
            return await super().one(collection, query)

    store = _StoreRunner()
    runner = RemoteRunner(store)
    order = []

    class _Commands:
        async def run(self, command, cwd=None, user=None, timeout=None, envs=None):
            class _R:
                exit_code = 0
                stdout = "ok"
                stderr = ""
            return _R()

    class _Sandbox:
        sandbox_id = "sbx_1"

        def __init__(self):
            self.commands = _Commands()

        async def update_network(self, cfg):
            order.append(("update_network", cfg))

        async def kill(self):
            return None

    class _AsyncSandbox:
        @staticmethod
        async def create(**kwargs):
            order.append(("create", kwargs))
            return _Sandbox()

    fake_e2b = types.SimpleNamespace(AsyncSandbox=_AsyncSandbox, CommandExitException=Exception)
    monkeypatch.setitem(sys.modules, "e2b", fake_e2b)
    monkeypatch.setattr("bounty.runner.SecretVault.get", lambda self, name: "e2b_test_key")

    async def fake_command(command, cwd=None, check=True):
        order.append(("command", command, cwd))
        return {"exit_code": 0, "output": "ok"}

    async def fake_git(*args, cwd=None):
        order.append(("git", args, cwd))
        return "ok"

    runner.command = fake_command
    runner.git = fake_git

    _run(runner._create({"id": "job_1", "base_sha": "abc", "branch": "penniless/job_1"}, {"repository": "owner/repo", "default_branch": "main"}))

    sequence = [x[0] for x in order]
    assert "git" in sequence and "update_network" in sequence
    first_clone = next(i for i, item in enumerate(order) if item[0] == "git" and "clone" in item[1])
    net_idx = next(i for i, item in enumerate(order) if item[0] == "update_network")
    first_checkout = next(i for i, item in enumerate(order) if item[0] == "git" and "checkout" in item[1])
    assert first_clone < net_idx < first_checkout


def test_runner_command_sends_safe_env_only(monkeypatch):
    os.environ["BOUNTY_REMOTE_WORKDIR"] = "/work"
    os.environ["RUNNER_COMMAND_TIMEOUT_SECONDS"] = "25"
    captured = {}

    class _Commands:
        async def run(self, command, cwd=None, user=None, timeout=None, envs=None):
            captured["envs"] = envs
            class _R:
                exit_code = 0
                stdout = "ok"
                stderr = ""
            return _R()

    class _Sandbox:
        def __init__(self):
            self.commands = _Commands()

    store = _Store()
    runner = RemoteRunner(store)
    runner.sandbox = _Sandbox()
    runner.job_id = "job_1"
    _run(runner.command("git status", cwd="/work/repository"))

    assert "GIT_TERMINAL_PROMPT" in captured["envs"]
    assert "GITHUB_TOKEN" not in captured["envs"]
    assert "EXPLABS_API_KEY" not in captured["envs"]


def test_live_worker_run_reaches_waiting_approval_and_no_remote_publish(monkeypatch):
    os.environ["RUNNER_HARD_TIMEOUT_SECONDS"] = "60"

    class _Orchestrator:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.tasks = {}
            self.steps = []

        async def step(self, job, status, agent, reason):
            self.steps.append((status, agent, reason))

    class _StoreWorker(_Store):
        async def settings(self):
            return {"max_execution_time": 60}

        async def insert(self, collection, doc):
            saved = dict(doc)
            saved.setdefault("id", f"{collection}_{len(self.inserted)+1}")
            self.inserted.append((collection, saved))
            if collection == "pull_requests":
                self._rows["pull_requests"][saved["id"]] = saved
            return saved

    class _GitHubWorker:
        async def request(self, method, path, private=False):
            if "/git/trees/" in path:
                return {"truncated": False, "tree": [{"path": "src/main.py", "type": "blob", "mode": "100644", "size": 20, "sha": "blob1"}]}
            if "/git/blobs/" in path:
                import base64
                return {"content": base64.b64encode(b"print('safe')\n").decode()}
            return {"object": {"sha": "base-sha"}}

    class _Runner:
        def __init__(self, store):
            self.store = store
            self.workdir = "/work"

            class _Files:
                async def read(self, path, user=None):
                    return "print('patched')\n"

            class _S:
                files = _Files()

            self.sandbox = _S()

        async def open(self, job, conn):
            return None

        async def tests(self, suite):
            return {"exit_code": 0, "output": "1 passed"}

        async def write(self, path, content):
            return None

        async def git(self, *args, cwd=None):
            command = " ".join(args)
            if "diff --cached" in command:
                return "DIFF"
            if "diff --no-ext-diff --no-textconv" in command:
                return ""
            if "rev-parse" in command:
                return "commit-sha"
            return ""

        async def safe_target(self, path):
            return None

        async def close(self):
            return None

    class _Gateway:
        def __init__(self, store):
            self.store = store

        async def invoke(self, role, messages, job_id, max_tokens):
            if role == "coding":
                return json.dumps({"summary": "Fix issue", "files": {"src/main.py": "print('patched')\n"}})
            if role == "security":
                return json.dumps({"status": "PASS", "findings": []})
            return json.dumps({"status": "APPROVE", "findings": []})

    monkeypatch.setattr("bounty.live_worker.RemoteRunner", _Runner)
    monkeypatch.setattr("bounty.live_worker.ExperientialGateway", _Gateway)

    store = _StoreWorker()
    orchestrator = _Orchestrator()
    worker = LiveWorker(store, orchestrator, _GitHubWorker(), connections=None)
    job = {
        "id": "job_1",
        "repository": "owner/repo",
        "issue_number": 7,
        "title": "Issue",
        "issue_body": "Body",
        "base_sha": "base-sha",
        "base_branch": "main",
        "branch": "penniless/job_1",
        "test_suite": "pytest",
        "status": "SELECTED",
        "patch_files": {},
    }

    _run(worker.run(job, {"repository": "owner/repo"}))

    assert any(step[0] == "WAITING_APPROVAL" for step in orchestrator.steps)
    inserted_types = [c for c, _ in store.inserted]
    assert "pull_requests" in inserted_types
    assert "approval_requests" in inserted_types
    assert not any("/pulls" in str(a) or "/git/refs" in str(a) for a in store.audits)
