"""Iteration 5 isolated unit tests for Experiential gateway transport contracts."""

# Modules/features covered: cost_nano/micro conversion, missing-cost pending semantics, single-dispatch error handling.
import asyncio
import os

import httpx
import pytest
from fastapi import HTTPException

import sys

sys.path.append("/app/backend")
from bounty import gateway as gateway_module
from bounty.gateway import ExperientialGateway, confirmed_nano_cost


class _Collection:
    def __init__(self):
        self.calls = []

    async def update_one(self, query, update, upsert=False):
        self.calls.append({"query": query, "update": update, "upsert": upsert})


class _DB:
    def __init__(self):
        self.model_usage = _Collection()
        self.integration_connections = _Collection()


class _Store:
    def __init__(self):
        self.db = _DB()
        self.inserted = []
        self.audits = []

    async def controls(self):
        return {
            "global_stop": False,
            "integrations_revoked": False,
            "spending_stopped": False,
            "agents_stopped": False,
        }

    async def insert(self, collection, doc):
        row = {"id": "usage_1", **doc}
        self.inserted.append((collection, row))
        return row

    async def audit(self, *args, **kwargs):
        self.audits.append({"args": args, "kwargs": kwargs})


def _run(coro):
    return asyncio.run(coro)


def test_confirmed_nano_cost_prefers_nano_then_micro_conversion():
    assert confirmed_nano_cost({"usage": {"cost_nano_usd": 123}}) == 123
    assert confirmed_nano_cost({"usage": {"cost_micro_usd": 7}}) == 7000
    assert confirmed_nano_cost({"cost_micro_usd": 2}) == 2000


def test_confirmed_nano_cost_missing_or_invalid_returns_none():
    assert confirmed_nano_cost({}) is None
    assert confirmed_nano_cost({"usage": {"cost_nano_usd": -1}}) is None
    assert confirmed_nano_cost({"usage": {"cost_nano_usd": True}}) is None


def test_dispatch_success_sets_provider_confirmed_cost(monkeypatch):
    os.environ.setdefault("EXPLABS_REQUEST_TIMEOUT_SECONDS", "20")
    os.environ.setdefault("EXPLABS_BASE_URL", "https://example.test")

    store = _Store()
    gateway = ExperientialGateway(store)

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost_micro_usd": 3},
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(gateway_module, "experiential_key", lambda: "token")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _Client())

    out = _run(gateway._dispatch("model-a", [{"role": "user", "content": "hi"}], "job1", "coding", 50))
    assert out == "ok"
    updates = store.db.model_usage.calls
    assert updates[-1]["update"]["$set"]["actual_cost_nano_usd"] == 3000
    assert updates[-1]["update"]["$set"]["cost_status"] == "PROVIDER_CONFIRMED"


def test_dispatch_success_without_cost_keeps_pending(monkeypatch):
    os.environ.setdefault("EXPLABS_REQUEST_TIMEOUT_SECONDS", "20")
    os.environ.setdefault("EXPLABS_BASE_URL", "https://example.test")

    store = _Store()
    gateway = ExperientialGateway(store)

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(gateway_module, "experiential_key", lambda: "token")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _Client())

    out = _run(gateway._dispatch("model-a", [{"role": "user", "content": "hi"}], "job1", "coding", 50))
    assert out == "ok"
    update = store.db.model_usage.calls[-1]["update"]["$set"]
    assert update["actual_cost_nano_usd"] is None
    assert update["cost_status"] == "PENDING"


def test_dispatch_http_error_fails_once_without_retry(monkeypatch):
    os.environ.setdefault("EXPLABS_REQUEST_TIMEOUT_SECONDS", "20")
    os.environ.setdefault("EXPLABS_BASE_URL", "https://example.test")

    store = _Store()
    gateway = ExperientialGateway(store)
    count = {"post": 0}

    class _Resp:
        status_code = 502

        @staticmethod
        def json():
            return {"error": "bad gateway"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            count["post"] += 1
            return _Resp()

    monkeypatch.setattr(gateway_module, "experiential_key", lambda: "token")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _Client())

    with pytest.raises(HTTPException) as exc:
        _run(gateway._dispatch("model-a", [{"role": "user", "content": "hi"}], "job1", "coding", 50))
    assert exc.value.status_code == 502
    assert count["post"] == 1
    fail_update = store.db.model_usage.calls[-1]["update"]["$set"]
    assert fail_update["status"] == "FAILED_OR_AMBIGUOUS"
    assert fail_update["cost_status"] == "PENDING"


def test_dispatch_timeout_marks_ambiguous_and_raises(monkeypatch):
    os.environ.setdefault("EXPLABS_REQUEST_TIMEOUT_SECONDS", "20")
    os.environ.setdefault("EXPLABS_BASE_URL", "https://example.test")

    store = _Store()
    gateway = ExperientialGateway(store)
    count = {"post": 0}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            count["post"] += 1
            raise httpx.RequestError("timeout")

    monkeypatch.setattr(gateway_module, "experiential_key", lambda: "token")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _Client())

    with pytest.raises(HTTPException) as exc:
        _run(gateway._dispatch("model-a", [{"role": "user", "content": "hi"}], "job1", "coding", 50))
    assert exc.value.status_code == 502
    assert count["post"] == 1
    assert store.db.model_usage.calls[-1]["update"]["$set"]["status"] == "FAILED_OR_AMBIGUOUS"
