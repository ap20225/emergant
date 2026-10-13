"""Read-only Command Center aggregation. Pure observability.
NEVER authorizes execution, reserves budget, launches E2B, calls a model, or mutates any repository/state.
It only reads existing collections and existing readiness/finance services (single source of truth)."""
import os

from .autonomy import ESCALATION_LIMITS
from .finance import financials
from .readiness import execution_status
from .store import now

OPPORTUNITY_STATUSES = ['DISCOVERED', 'AWAITING_AUTHORIZATION', 'STALE_REQUIRES_RECHECK', 'REJECTED', 'COMPLETED']
JOB_ACTIVE = ['SELECTED', 'TRIAGE', 'RESEARCHING', 'CODING', 'TESTING', 'REVIEW', 'READY']
OPP_FIELDS = ['id', 'repository', 'issue_number', 'title', 'source', 'source_url', 'status',
              'qualification_status', 'qualification_reason', 'reward', 'currency',
              'reward_verification_status', 'risk_status', 'authorization_state',
              'duplicate_key', 'recheck_reason', 'created_at', 'last_checked_at']
JOB_FIELDS = ['id', 'title', 'repository', 'status', 'simulated', 'test_status', 'security_status',
              'review_status', 'approval_status', 'pr_url', 'created_at', 'updated_at']


async def command_center(store):
    readiness = await execution_status(store)
    db = store.db

    # ---- SYSTEM (single source of truth: readiness + persisted scheduler/discovery state) ----
    sched = (await store.one('system_settings', {'id': 'scheduler-state'}) or {}).get('config') or {}
    disc_cfg = (await store.one('system_settings', {'id': 'discovery-config'}) or {}).get('config') \
        or {'query': 'is:issue is:open label:bounty', 'per_page': 20, 'enabled': False}
    system = {
        'status': readiness['status'],
        'capability_matrix': readiness['capability_matrix'],
        'feature_flags': readiness['feature_flags'],
        'blockers': readiness['blockers'],
        'scheduler': {
            'background_enabled': os.environ.get('SCHEDULER_BACKGROUND_ENABLED') == 'true',
            'tick_seconds': int(os.environ.get('SCHEDULER_TICK_SECONDS', '900')),
            'last_started_at': sched.get('last_started_at'),
            'last_finished_at': sched.get('last_finished_at'),
            'lease_until': sched.get('lease_until'),
            'holder': sched.get('holder'),
        },
        'discovery_config': disc_cfg,
    }

    # ---- SOURCES ----
    sources = await store.rows('source_health', {}, limit=50)

    # ---- OPPORTUNITIES ----
    opp_counts = {s: await db.bounty_opportunities.count_documents({'status': s}) for s in OPPORTUNITY_STATUSES}
    opp_counts['QUALIFIED'] = await db.bounty_opportunities.count_documents({'qualification_status': 'QUALIFIED'})
    opp_items = await store.rows('bounty_opportunities', {}, limit=100)
    opportunities = {
        'counts': opp_counts,
        'total': await db.bounty_opportunities.count_documents({}),
        'items': [{k: o.get(k) for k in OPP_FIELDS} for o in opp_items],
    }

    # ---- FINANCE (RunBudget + provider-confirmed usage + live ledger; reserved != spent) ----
    run_budget = readiness['run_budget']
    cap = int(run_budget.get('cap_nano_usd') or 0)
    reserved = int(run_budget.get('spent_nano_usd') or 0)
    confirmed = 0
    async for u in db.model_usage.find({}, {'committed_nano_usd': 1, '_id': 0}):
        confirmed += int(u.get('committed_nano_usd') or 0)
    live = await financials(store, simulated=False)
    escalation_count = await db.escalations.count_documents({})
    finance = {
        'run_budget': run_budget,
        'cap_nano_usd': cap,
        'reserved_nano_usd': reserved if run_budget.get('authorized') else 0,
        'provider_confirmed_spent_nano_usd': confirmed,
        'remaining_nano_usd': (cap - reserved) if run_budget.get('authorized') else cap,
        'escalation': {
            'enabled': readiness['feature_flags']['AUTO_ESCALATION_ENABLED'],
            'count': escalation_count,
            'spend_nano_usd': 'NOT_TRACKED',
            'max_cost_per_job_nano_usd': ESCALATION_LIMITS['max_cost_per_job_nano_usd'],
        },
        'revenue': {
            'verified_revenue': live['verified_revenue'],
            'verified_payments': live['verified_payments'],
            'pending_receipts': live['pending_receipts'],
        },
        'model_requests': live['model_requests'],
    }

    # ---- EXECUTION ----
    job_counts = {
        'ACTIVE': await db.jobs.count_documents({'status': {'$in': JOB_ACTIVE}}),
        'WAITING_APPROVAL': await db.jobs.count_documents({'status': 'WAITING_APPROVAL'}),
        'APPROVED': await db.jobs.count_documents({'status': 'APPROVED'}),
        'NEEDS_REVIEW': await db.jobs.count_documents({'status': 'NEEDS_REVIEW'}),
        'BLOCKED': await db.jobs.count_documents({'status': 'BLOCKED'}),
        'FAILED': await db.jobs.count_documents({'status': 'FAILED'}),
        'SUBMITTED': await db.jobs.count_documents({'status': 'SUBMITTED'}),
        'CANCELLED': await db.jobs.count_documents({'status': 'CANCELLED'}),
        'COMPLETED': await db.jobs.count_documents({'status': {'$in': ['MERGED', 'PAID']}}),
    }
    job_items = await store.rows('jobs', {}, limit=25)
    execution = {
        'counts': job_counts,
        'awaiting_approval': await db.approval_requests.count_documents({'status': 'WAITING_FOR_APPROVAL'}),
        'pull_requests': {
            'staged': await db.pull_requests.count_documents({'status': {'$in': ['AWAITING_PUBLICATION', 'LOCAL_DRAFT']}}),
            'published': await db.pull_requests.count_documents({'external': True}),
        },
        'jobs': [{k: j.get(k) for k in JOB_FIELDS} for j in job_items],
    }

    # ---- SAFETY (authoritative counts only; explicit NOT_TRACKED where no persisted source exists) ----
    safety = {
        'prompt_injection_rejects': await db.bounty_opportunities.count_documents({'qualification_reason': {'$regex': 'prompt-injection'}}),
        'unsafe_content_rejects': await db.bounty_opportunities.count_documents({'risk_status': 'FLAGGED'}),
        'unsafe_patch_rejects': await db.security_reviews.count_documents({'status': 'FAIL'}),
        'secret_violations': 'NOT_TRACKED',
        'publication_attempts': await db.publication_attempts.count_documents({}),
        'successful_publications': await db.publication_attempts.count_documents({'status': 'CONFIRMED'}),
        'blocked_publications': await db.publication_attempts.count_documents({'status': 'FAILED_OR_AMBIGUOUS'}),
        'authorization_denials': 'NOT_TRACKED',
        'blocked_events': await db.audit_logs.count_documents({'result': 'BLOCKED'}),
    }

    return {'generated_at': now(), 'system': system, 'sources': sources,
            'opportunities': opportunities, 'finance': finance,
            'execution': execution, 'safety': safety}
