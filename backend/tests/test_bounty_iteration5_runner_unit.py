"""Iteration 5 isolated unit tests for remote runner fail-closed contracts."""

# Modules/features covered: runner preflight fail-closed gates, protected patch path validation, host-exec guardrails.
import asyncio
import os

import pytest
from fastapi import HTTPException

import sys

sys.path.append("/app/backend")
from bounty.patches import safe_path
from bounty.runner import RemoteRunner
import bounty.runner as runner_module


class _Store:
    def __init__(self, controls=None, settings=None, runner_connection=None):
        self._controls = controls or {
            "global_stop": False,
            "agents_stopped": False,
            "spending_stopped": False,
            "integrations_revoked": False,
        }
        self._settings = settings or {"mode": "ASSISTED", "authorized_budget": 0}
        self._runner_connection = runner_connection

    async def controls(self):
        return self._controls

    async def settings(self):
        return self._settings

    async def one(self, collection, query):
        if collection == "integration_connections" and query == {"id": "runner"}:
            return self._runner_connection
        return None


def _run(coro):
    return asyncio.run(coro)


def test_preflight_requires_runner_connection_and_secret(monkeypatch):
    os.environ.setdefault("BOUNTY_REMOTE_WORKDIR", "/tmp/work")
    os.environ.setdefault("RUNNER_SPEND_AUTHORIZED", "false")
    store = _Store(runner_connection=None)
    runner = RemoteRunner(store)

    monkeypatch.setattr(runner_module.SecretVault, "has", lambda self, name: False)

    with pytest.raises(HTTPException) as exc:
        _run(runner.preflight())
    assert exc.value.status_code == 409
    assert "RUNNER_REQUIRED" in exc.value.detail


def test_preflight_remains_fail_closed_even_when_configured(monkeypatch):
    os.environ.setdefault("BOUNTY_REMOTE_WORKDIR", "/tmp/work")
    os.environ.setdefault("PENNILESS_SECRETS_DIR", "/tmp/penniless-secrets-test")
    os.environ["RUNNER_SPEND_AUTHORIZED"] = "true"
    store = _Store(
        settings={"mode": "ASSISTED", "authorized_budget": 1},
        runner_connection={"id": "runner", "template_id": "tmpl_abc"},
    )
    runner = RemoteRunner(store)

    monkeypatch.setattr(runner_module.SecretVault, "has", lambda self, name: True)

    with pytest.raises(HTTPException) as exc:
        _run(runner.preflight())
    assert exc.value.status_code == 409
    assert "RUNNER_COST_CEILING_REQUIRED" in exc.value.detail


@pytest.mark.parametrize(
    "path",
    [
        "../secrets.txt",
        ".git/config",
        ".github/workflows/ci.yml",
        ".env",
        ".env.local",
        "/abs/path.py",
        "dir/../../escape.py",
        "key.pem",
    ],
)
def test_safe_path_blocks_traversal_and_protected_targets(path):
    with pytest.raises(HTTPException) as exc:
        safe_path(path)
    assert exc.value.status_code == 422


def test_runner_source_has_no_host_subprocess_execution():
    content = open("/app/backend/bounty/runner.py", "r", encoding="utf-8").read()
    assert "subprocess" not in content
    assert "AsyncSandbox.create" in content
    assert "commands.run" in content
