import os
from .secrets import SecretVault, experiential_key
from .store import now
from .gateway import CAP_REASON


async def execution_status(store):
    rows = {r['id']: r for r in await store.rows('integration_connections')}
    github, model, runner = (rows.get(k, {}) for k in ['github', 'experiential', 'runner'])
    controls, settings = await store.controls(), await store.settings()
    vault = SecretVault()
    blocked = []
    def add(code, action):
        blocked.append({'code': code, 'action': action})
    if any(controls.get(k) for k in ['global_stop', 'agents_stopped', 'integrations_revoked', 'submissions_disabled', 'spending_stopped']):
        add('EMERGENCY_POLICY', 'Review emergency controls before resuming any operation.')
    if settings['mode'] != 'ASSISTED':
        add('ASSISTED_REQUIRED', 'Select ASSISTED mode; unrestricted execution is not supported.')
    gh = github.get('status') == 'CONNECTED' and vault.has('github')
    if not gh:
        add('GITHUB_REQUIRED', 'Security → Connect GitHub: enter one public repository you own, an open issue number and a read-only fine-grained token.')
    runner_attestation = await store.one('execution_attestations', {'id': 'runner-verification'})
    runner_verified = bool(runner_attestation and runner_attestation.get('verified'))
    if not vault.has('e2b') or not runner.get('template_id'):
        add('RUNNER_REQUIRED', 'Configure an E2B account/API key and dependency-ready sandbox template in Security. No host execution fallback.')
    elif not runner_verified:
        add('RUNNER_UNVERIFIED', 'Configured E2B template has not been runtime-verified; run the E2B runner verification in Security.')
    if not model.get('inference_verified_at'):
        add('MODEL_INFERENCE_UNVERIFIED', 'No inference has completed yet. A controlled run performs the first real, cost-capped inference.')
    from .budget import RunBudget
    run_budget = await RunBudget(store).state()
    from .autonomy import get_flags
    flags = await get_flags(store)
    scheduler_background = os.environ.get('SCHEDULER_BACKGROUND_ENABLED') == 'true'
    gh_health = await store.one('source_health', {'id': 'github'})
    real_source = ('READY' if (gh_health and gh_health.get('status') == 'HEALTHY' and gh_health.get('total_discovered'))
                   else ('PARTIAL' if gh_health else 'NOT_IMPLEMENTED'))
    if not model.get('model_ids'):
        add('CATALOG_UNVERIFIED', 'Refresh the Experiential Labs catalog before routing any inference.')
    if not run_budget.get('authorized'):
        add('RUN_BUDGET_NOT_AUTHORIZED', 'No controlled-run spend cap is authorized. Starting one run authorizes an app-enforced $1.00 cumulative ceiling; no ongoing spend.')
    proof = await store.one('execution_attestations', {'id': 'controlled-real-pipeline', 'verified': True})
    if not proof:
        add('CONTROLLED_RUN_REQUIRED', 'After connections and budget gates pass, approve one real issue → tests → reviews → draft PR proof.')
    capabilities = {'github': 'CONNECTED' if gh else 'NOT_CONNECTED',
        'model_gateway': 'INFERENCE_VERIFIED' if model.get('inference_verified_at') else model.get('status', 'NOT_CONNECTED'),
        'workspace': ('RUNTIME_VERIFIED' if runner_verified else ('CONFIGURED_UNVERIFIED' if vault.has('e2b') else 'RUNNER_REQUIRED')),
        'coding_worker': 'IMPLEMENTED_UNVERIFIED', 'testing': 'IMPLEMENTED_UNVERIFIED',
        'review': 'IMPLEMENTED_UNVERIFIED', 'pr': 'IMPLEMENTED_APPROVAL_REQUIRED'}
    return {'status': 'NOT_READY' if blocked else 'READY', 'capabilities': capabilities, 'blockers': blocked, 'checked_at': now(),
        'github': {k: v for k, v in github.items() if k not in ['_id', 'owner_user_id']},
        'model': {'provider': 'Experiential Labs', 'credential_configured': bool(experiential_key()),
                  'catalog_status': model.get('status', 'NOT_CHECKED'), 'model_count': model.get('model_count', 0),
                  'catalog_verified_at': model.get('catalog_verified_at'), 'inference_verified_at': model.get('inference_verified_at'),
                  'routing_strategy': 'CHEAPEST_FIRST_CAPPED', 'diagnostic_reason': CAP_REASON, 'model_ids': model.get('model_ids', []),
                  'pricing_estimates': model.get('pricing_estimates', {})},
        'runner': {**runner, 'credential_configured': vault.has('e2b'), 'provider': 'E2B',
                   'runtime_verified': runner_verified, 'attestation': runner_attestation},
        'run_budget': run_budget,
        'capability_matrix': {
            'CONTROLLED_OPERATION': 'READY' if (gh and runner_verified and model.get('model_ids')) else 'BLOCKED',
            'DISCOVERY': 'READY' if flags['DISCOVERY_ENABLED'] else 'DISABLED',
            'REAL_SOURCE': real_source,
            'QUALIFICATION': 'READY' if flags['QUALIFICATION_ENABLED'] else 'DISABLED',
            'LIVE_RECHECK': 'READY',
            'NIGHTLY_BRIEFING': 'READY' if flags['NIGHTLY_BRIEFING_ENABLED'] else 'DISABLED',
            'PERSISTENT_SCHEDULER': 'READY' if scheduler_background else 'PARTIAL',
            'AUTO_ESCALATION': 'DISABLED' if not flags['AUTO_ESCALATION_ENABLED'] else 'ENABLED',
            'UNATTENDED_EXECUTION': 'DISABLED' if not flags['AUTONOMOUS_EXECUTION_ENABLED'] else 'ENABLED',
            'REWARD_VERIFICATION': 'PARTIAL',
            'REPO_AUTHORIZATION': 'READY',
            'FORK_EXECUTION': 'PARTIAL',
            'AUTONOMOUS_AUTHORIZATION': 'READY',
        },
        'feature_flags': flags,
        'source_health': await store.rows('source_health', {}, limit=20),
        'usage': await store.rows('model_usage', limit=50),
        'diagnostic_attempted': bool(await store.one('model_usage', {'id': 'experiential-diagnostic-once'}))}