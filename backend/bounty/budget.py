"""Single-run model spend cap. App-enforced $1.00 ceiling, atomically race-safe; no provider pre-authorization hold."""
import os
from fastapi import HTTPException
from pymongo import ReturnDocument
from .store import now


class RunBudget:
    def __init__(self, store):
        self.store = store

    async def cap(self):
        from .execution_controls import get_budget
        return int((await get_budget(self.store))['per_job_cap_nano_usd'])

    def reserve_rate(self):
        return int(os.environ['RUN_BUDGET_RESERVE_RATE_NANO_PER_MILLION'])

    async def state(self):
        row = await self.store.one('system_settings', {'id': 'run-budget'})
        return row['config'] if row else {'authorized': False, 'spent_nano_usd': 0, 'cap_nano_usd': await self.cap(), 'run_id': None}

    async def authorize(self, actor, run_id):
        cap = await self.cap()
        config = {'authorized': True, 'spent_nano_usd': 0, 'cap_nano_usd': cap,
                  'run_id': run_id, 'authorized_at': now(), 'authorized_by': actor}
        await self.store.db.system_settings.update_one({'id': 'run-budget'}, {'$set': {'config': config}}, upsert=True)
        await self.store.audit('finance', 'Controlled run budget authorized',
            f'Single run {run_id}; app-enforced hard per-job cap {cap} nano-USD; no retries, no auto-increase', actor=actor)
        return config

    def worst_case(self, prompt_chars, max_tokens):
        # Conservative worst-case: bounded output plus estimated prompt tokens at a floor rate.
        tokens = prompt_chars // 4 + int(max_tokens)
        return -(-tokens * self.reserve_rate() // 1_000_000)

    async def reserve(self, reservation_nano):
        """Atomically PRE-CHARGE the worst-case reservation before any paid call.
        Race-safe: two concurrent callers cannot both pass when the remaining cap is insufficient."""
        reservation = int(reservation_nano)
        doc = await self.store.db.system_settings.find_one_and_update(
            {'id': 'run-budget', 'config.authorized': True,
             '$expr': {'$lte': [{'$add': ['$config.spent_nano_usd', reservation]}, '$config.cap_nano_usd']}},
            {'$inc': {'config.spent_nano_usd': reservation}}, return_document=ReturnDocument.AFTER)
        if not doc:
            st = await self.state()
            if not st.get('authorized'):
                raise HTTPException(409, 'RUN_NOT_AUTHORIZED: no controlled-run spend cap authorized; no request sent')
            remaining = st['cap_nano_usd'] - st['spent_nano_usd']
            raise HTTPException(409, f'BUDGET_CAP_WOULD_EXCEED: worst-case {reservation} > remaining {remaining} nano-USD; call blocked, no spend')
        return doc['config']

    async def reconcile(self, reservation_nano, actual_nano):
        """After the call, adjust the pre-charged reservation to the provider-confirmed actual cost.
        If the provider returned no cost, the conservative reservation stays charged."""
        if actual_nano is None:
            return
        delta = int(actual_nano) - int(reservation_nano)
        if delta:
            await self.store.db.system_settings.update_one({'id': 'run-budget'}, {'$inc': {'config.spent_nano_usd': delta}})

    async def close(self):
        await self.store.db.system_settings.update_one({'id': 'run-budget'}, {'$set': {'config.authorized': False, 'config.closed_at': now()}})
