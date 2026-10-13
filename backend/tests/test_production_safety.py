"""Deterministic production-safety tests. NO provider/model calls, NO E2B, NO real pipeline run."""
import asyncio
import json
import os
import sys
import pytest
sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import HTTPException
from bounty.store import Store, redact
from bounty.budget import RunBudget
from bounty.patches import parse_patch


def _store():
    return Store(AsyncIOMotorClient(os.environ['MONGO_URL'])[os.environ['DB_NAME']])


def test_budget_cap_is_race_safe():
    async def run():
        b = RunBudget(_store())
        await b.authorize('test', 'race')
        cap = (await b.state())['cap_nano_usd']
        await b.store.db.system_settings.update_one({'id': 'run-budget'}, {'$set': {'config.spent_nano_usd': cap - 1000}})

        async def reserve():
            try:
                await b.reserve(800)
                return 'OK'
            except HTTPException:
                return 'BLOCKED'
        results = await asyncio.gather(*[reserve() for _ in range(8)])
        final = (await b.state())['spent_nano_usd']
        await b.close()
        return results, final, cap
    results, final, cap = asyncio.run(run())
    assert results.count('OK') == 1, results   # exactly one concurrent call may proceed
    assert final <= cap                         # never overspent


def test_reserve_blocked_when_not_authorized():
    async def run():
        b = RunBudget(_store())
        await b.close()
        try:
            await b.reserve(1)
            return False
        except HTTPException:
            return True
    assert asyncio.run(run()) is True


def test_reconcile_adjusts_to_actual():
    async def run():
        b = RunBudget(_store())
        await b.authorize('test', 'reconcile')
        await b.reserve(10_000)
        await b.reconcile(10_000, 500)
        spent = (await b.state())['spent_nano_usd']
        await b.close()
        return spent
    assert asyncio.run(run()) == 500


def test_redact_catches_secrets():
    assert 'REDACTED' in redact('key AKIAIOSFODNN7EXAMPLE end')
    assert 'REDACTED_PRIVATE_KEY' in redact('-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----')
    assert 'REDACTED' in redact('token=abcdefhijklmnop')


def test_parse_patch_rejects_secrets_and_unsafe_paths():
    parse_patch(json.dumps({'files': {'feature.py': 'def f():\n    return 1\n'}}))
    for bad in [{'feature.py': 'K = "AKIAIOSFODNN7EXAMPLE"\n'}, {'.env': 'X=1\n'}, {'../escape.py': 'x=1\n'}]:
        with pytest.raises(HTTPException):
            parse_patch(json.dumps({'files': bad}))
