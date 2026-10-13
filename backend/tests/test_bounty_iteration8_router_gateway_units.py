"""Iteration 8 isolated unit tests for free-first router and gateway guards."""

# Modules/features covered: free-eligibility interpretation, bounded distinct attempts,
# paid-disabled escalation behavior, and dispatch guard requiring fresh routing decision.
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

sys.path.append("/app/backend")
from bounty.gateway import ExperientialGateway
from bounty.model_router import ModelRouter
from bounty.routing_policy import RoutingPolicy, current_free_models, inspect_free_metadata


def _run(coro):
    return asyncio.run(coro)


def _iso_now(offset_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


class _Collection:
    def __init__(self):
        self.calls = []

    async def update_one(self, query, update, upsert=False):
        self.calls.append({"query": query, "update": update, "upsert": upsert})


class _DB:
    def __init__(self):
        self.model_usage = _Collection()
        self.routing_decisions = _Collection()
        self.integration_connections = _Collection()


class _Store:
    def __init__(self, catalog=None, policy=None):
        self.db = _DB()
        self.catalog = catalog or {}
        self.policy = policy or RoutingPolicy().model_dump()
        self.usage_rows = []
        self.routing_rows = []
        self.audits = []

    async def controls(self):
        return {
            "global_stop": False,
            "agents_stopped": False,
            "spending_stopped": False,
            "integrations_revoked": False,
        }

    async def one(self, collection, query):
        if collection == "system_settings" and query == {"id": "model-routing"}:
            return {"id": "model-routing", "config": self.policy, "updated_at": _iso_now()}
        if collection == "integration_connections" and query == {"id": "experiential"}:
            return self.catalog
        return None

    async def rows(self, collection, query=None, limit=250):
        if collection == "model_usage":
            items = self.usage_rows
            if query:
                items = [r for r in items if all(r.get(k) == v for k, v in query.items())]
            return list(reversed(items))[:limit]
        if collection == "routing_decisions":
            return list(reversed(self.routing_rows))[:limit]
        return []

    async def insert(self, collection, doc):
        row = {"id": f"{collection}_{len(self.routing_rows if collection == 'routing_decisions' else self.usage_rows) + 1}", **doc}
        if collection == "routing_decisions":
            self.routing_rows.append(row)
        elif collection == "model_usage":
            self.usage_rows.append(row)
        return row

    async def audit(self, *args, **kwargs):
        self.audits.append({"args": args, "kwargs": kwargs})


def test_current_free_models_uses_verified_free_and_fresh_catalog_only(monkeypatch):
    monkeypatch.setenv("FREE_ROUTE_MAX_EVIDENCE_AGE_SECONDS", "60")

    fresh_catalog = {
        "status": "CATALOG_CONNECTED",
        "catalog_verified_at": _iso_now(),
        "model_ids": ["a", "b", "c"],
        "free_eligibility": {
            "a": {"status": "VERIFIED_FREE"},
            "b": {"status": "UNVERIFIED"},
            "c": {"status": "VERIFIED_FREE"},
        },
    }
    stale_catalog = {**fresh_catalog, "catalog_verified_at": _iso_now(offset_seconds=-500)}
    provider_error = {**fresh_catalog, "status": "PROVIDER_ERROR"}

    assert current_free_models(fresh_catalog) == ["a", "c"]
    assert current_free_models(stale_catalog) == []
    assert current_free_models(provider_error) == []


def test_inspect_free_metadata_marks_unverified_for_estimates_byok_unknowns():
    item = {
        "providers": [
            {
                "pricing_source": "estimate",
                "available_to_org": None,
                "routable": True,
                "billing_source": "byok",
                "hidden_fallback": True,
                "requires_payment": None,
                "requires_purchase": False,
                "input_nano_usd_per_million": 0,
                "output_nano_usd_per_million": 0,
            }
        ]
    }
    out = inspect_free_metadata(item)

    assert out["status"] == "UNVERIFIED"
    assert "Customer-managed/unknown billing is not free" in out["reasons"]
    assert "Pricing is estimated or has unknown provenance" in out["reasons"]
    assert "No authoritative route-complete zero-charge billing assertion" in out["reasons"]


def test_router_positive_path_selects_preferred_then_distinct_second_attempt(monkeypatch):
    monkeypatch.setenv("FREE_ROUTE_MAX_EVIDENCE_AGE_SECONDS", "120")
    policy = RoutingPolicy().model_dump()
    policy["coding"] = ["qwen3-coder-next", "gpt-oss-20b"]
    policy["max_free_attempts"] = 2
    catalog = {
        "status": "CATALOG_CONNECTED",
        "catalog_verified_at": _iso_now(),
        "model_ids": ["qwen3-coder-next", "gpt-oss-20b", "gpt-5.6-luna"],
        "free_eligibility": {
            "qwen3-coder-next": {"status": "VERIFIED_FREE"},
            "gpt-oss-20b": {"status": "VERIFIED_FREE"},
            "gpt-5.6-luna": {"status": "UNVERIFIED"},
        },
    }
    store = _Store(catalog=catalog, policy=policy)
    router = ModelRouter(store)

    first = _run(router.authorize("coding", "job-8", messages=[{"role": "user", "content": "approach-a"}]))
    assert first["result"] == "FREE_SELECTED"
    assert first["model"] == "qwen3-coder-next"
    assert first["estimated_cost_nano_usd"] == 0
    assert first["prompt_tokens"] is None
    assert first["completion_tokens"] is None

    store.usage_rows.append(
        {
            "task_key": "job-8:coding",
            "attempt_number": 1,
            "model": first["model"],
            "status": "SUCCEEDED",
            "actual_cost_nano_usd": 0,
            "task_result": "tests_failed",
            "approach_hash": first["approach_hash"],
        }
    )

    second = _run(
        router.authorize(
            "coding",
            "job-8",
            messages=[{"role": "user", "content": "approach-b"}],
            failure_reason="tests_failed",
        )
    )
    assert second["result"] == "FREE_SELECTED"
    assert second["model"] == "gpt-oss-20b"
    assert second["model"] != first["model"]


def test_router_enforces_two_attempt_limit_and_paid_candidates_remain_disabled(monkeypatch):
    monkeypatch.setenv("FREE_ROUTE_MAX_EVIDENCE_AGE_SECONDS", "120")
    policy = RoutingPolicy().model_dump()
    policy["coding"] = ["qwen3-coder-next", "gpt-oss-20b"]
    policy["paid_escalation"] = ["gpt-5.6-luna", "gpt-5.6-sol"]
    catalog = {
        "status": "CATALOG_CONNECTED",
        "catalog_verified_at": _iso_now(),
        "model_ids": ["qwen3-coder-next", "gpt-oss-20b", "gpt-5.6-luna", "gpt-5.6-sol"],
        "free_eligibility": {
            "qwen3-coder-next": {"status": "VERIFIED_FREE"},
            "gpt-oss-20b": {"status": "VERIFIED_FREE"},
        },
    }
    store = _Store(catalog=catalog, policy=policy)
    router = ModelRouter(store)

    first = _run(router.authorize("coding", "job-cap", messages=[{"role": "user", "content": "a"}]))
    store.usage_rows.append(
        {
            "task_key": "job-cap:coding",
            "attempt_number": 1,
            "model": first["model"],
            "status": "SUCCEEDED",
            "actual_cost_nano_usd": 0,
            "approach_hash": first["approach_hash"],
        }
    )
    second = _run(
        router.authorize(
            "coding",
            "job-cap",
            messages=[{"role": "user", "content": "b"}],
            failure_reason="invalid_patch",
        )
    )
    store.usage_rows.append(
        {
            "task_key": "job-cap:coding",
            "attempt_number": 2,
            "model": second["model"],
            "status": "SUCCEEDED",
            "actual_cost_nano_usd": 0,
            "approach_hash": second["approach_hash"],
        }
    )

    with pytest.raises(HTTPException) as exc:
        _run(
            router.authorize(
                "coding",
                "job-cap",
                messages=[{"role": "user", "content": "c"}],
                failure_reason="review_rejected",
            )
        )
    assert exc.value.status_code == 409
    assert "FREE_ATTEMPT_LIMIT_REACHED" in exc.value.detail

    final_decision = store.routing_rows[-1]
    assert final_decision["paid_execution"] == "DISABLED"
    assert final_decision["paid_candidates"] == ["gpt-5.6-luna", "gpt-5.6-sol"]
    assert final_decision["model"] is None


def test_router_blocks_repeated_output_and_never_retries_paid(monkeypatch):
    monkeypatch.setenv("FREE_ROUTE_MAX_EVIDENCE_AGE_SECONDS", "120")
    catalog = {
        "status": "CATALOG_CONNECTED",
        "catalog_verified_at": _iso_now(),
        "model_ids": ["qwen3-coder-next"],
        "free_eligibility": {"qwen3-coder-next": {"status": "VERIFIED_FREE"}},
    }
    store = _Store(catalog=catalog)
    store.usage_rows.append(
        {
            "task_key": "job-repeat:coding",
            "attempt_number": 1,
            "model": "qwen3-coder-next",
            "status": "SUCCEEDED",
            "actual_cost_nano_usd": 0,
            "task_result": "repeated_output",
            "approach_hash": "hash1",
        }
    )

    with pytest.raises(HTTPException) as exc:
        _run(
            ModelRouter(store).authorize(
                "coding",
                "job-repeat",
                messages=[{"role": "user", "content": "new"}],
                failure_reason="tests_failed",
            )
        )
    assert exc.value.status_code == 409
    assert "FATAL_TASK_FAILURE" in exc.value.detail
    assert store.routing_rows[-1]["paid_execution"] == "DISABLED"


def test_gateway_dispatch_without_fresh_decision_returns_409_before_http(monkeypatch):
    monkeypatch.setenv("FREE_ROUTE_MAX_EVIDENCE_AGE_SECONDS", "120")
    monkeypatch.setenv("EXPLABS_REQUEST_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("EXPLABS_BASE_URL", "https://example.test")

    called = {"http": 0}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            called["http"] += 1
            raise AssertionError("HTTP should not be called when decision is missing/invalid")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _Client())

    store = _Store(
        catalog={
            "status": "CATALOG_CONNECTED",
            "catalog_verified_at": _iso_now(),
            "model_ids": ["qwen3-coder-next"],
            "free_eligibility": {"qwen3-coder-next": {"status": "VERIFIED_FREE"}},
        }
    )
    gateway = ExperientialGateway(store)

    with pytest.raises(HTTPException) as exc:
        _run(gateway._dispatch("qwen3-coder-next", [{"role": "user", "content": "hi"}], "job-x", "coding", 64, decision=None))

    assert exc.value.status_code == 409
    assert "FREE_MODEL_UNVERIFIED" in exc.value.detail
    assert called["http"] == 0
