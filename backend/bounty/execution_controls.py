"""DB-backed autonomous execution budget controls (single source of truth).

Replaces the previous env-only per-run/per-job/daily caps. Persisted under
system_settings id='execution-controls'. Owner-editable via /api/bounty/execution/controls.
Defaults: master autonomous daily budget $25.00, per-job hard cap $0.25, max concurrent 1.
Budgets are never auto-increased.
"""
DEFAULTS = {
    'daily_budget_nano_usd': 25_000_000_000,   # $25.00 master autonomous ceiling per day
    'per_job_cap_nano_usd': 250_000_000,       # $0.25 hard per-job cap
    'max_concurrent_jobs': 1,
}


async def get_budget(store):
    row = await store.one('system_settings', {'id': 'execution-controls'})
    cfg = (row or {}).get('config') or {}
    return {**DEFAULTS, **{k: int(cfg[k]) for k in DEFAULTS if k in cfg and cfg[k] is not None}}


async def save_budget(store, patch, actor):
    current = await get_budget(store)
    current.update({k: int(v) for k, v in patch.items() if k in DEFAULTS and v is not None})
    await store.db.system_settings.update_one(
        {'id': 'execution-controls'}, {'$set': {'config': current}}, upsert=True)
    await store.audit('finance', 'Execution budget controls saved',
        f"daily={current['daily_budget_nano_usd']} per_job={current['per_job_cap_nano_usd']} "
        f"max_concurrent={current['max_concurrent_jobs']} nano-USD; no auto-increase", actor=actor)
    return current
