"""Editable preferences, never editable billing proof or spending authorization."""
import fnmatch
import os
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal
from .store import now


CODING = ['gpt-5.1-codex', 'qwen3-coder-flash', 'gpt-5.1-codex-max']
ROUTINE = ['qwen3.8-flash', 'gemini-3.5-flash']
FAILURES = ['invalid_patch', 'tests_failed', 'review_rejected']


class RoutingPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    routine: list[str] = Field(default_factory=lambda: list(ROUTINE), min_length=1, max_length=20)
    coding: list[str] = Field(default_factory=lambda: list(CODING), min_length=1, max_length=20)
    review: list[str] = Field(default_factory=lambda: ['qwen3.8-flash', 'gemini-3.5-flash'], min_length=1, max_length=20)
    paid_escalation: list[str] = Field(default_factory=lambda: ['gpt-5.6-luna', 'gpt-5.6-sol', 'claude-opus-*', 'gemini-3.5-pro'], max_length=20)
    max_free_attempts: int = Field(default=2, ge=1, le=2, strict=True)
    escalation_triggers: list[Literal['invalid_patch', 'tests_failed', 'review_rejected']] = Field(default_factory=lambda: list(FAILURES), max_length=3)

    @field_validator('routine', 'coding', 'review', 'paid_escalation')
    @classmethod
    def model_names(cls, values):
        import re
        if any(not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_./:*+-]{0,149}', value) for value in values) or len(set(values)) != len(values):
            raise ValueError('Use distinct model IDs or wildcard preferences, without spaces')
        return values


def role_group(role):
    if role in {'coding', 'coder', 'implementation'}:
        return 'coding'
    if role in {'review', 'security', 'tester', 'tests'}:
        return 'review'
    return 'routine'


def ordered_models(patterns, ids):
    result = []
    for pattern in patterns:
        result.extend(m for m in sorted(ids) if fnmatch.fnmatchcase(m, pattern) and m not in result)
    return result


def current_free_models(catalog):
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(catalog['catalog_verified_at'])).total_seconds()
        if catalog.get('status') != 'CATALOG_CONNECTED' or not 0 <= age <= int(os.environ['FREE_ROUTE_MAX_EVIDENCE_AGE_SECONDS']):
            return []
    except (KeyError, ValueError, TypeError):
        return []
    return [m for m in catalog.get('model_ids', []) if catalog.get('free_eligibility', {}).get(m, {}).get('status') == 'VERIFIED_FREE']


def inspect_free_metadata(item):
    """Raw catalog is NOT a zero-billing attestation. Unknown waterfall/provenance blocks."""
    providers = item.get('providers') or []
    reasons = []
    if not providers:
        reasons.append('No deployment metadata')
    for p in providers:
        if p.get('pricing_source') in {None, 'estimate'}:
            reasons.append('Pricing is estimated or has unknown provenance')
        if p.get('available_to_org') is not True or p.get('routable') is not True:
            reasons.append('Account availability is not explicitly verified')
        if p.get('billing_source') != 'host_managed':
            reasons.append('Customer-managed/unknown billing is not free')
        if p.get('hidden_fallback') is not False:
            reasons.append('Hidden fallback is present or unknown')
        if p.get('requires_payment') is not False or p.get('requires_purchase') is not False:
            reasons.append('Payment/purchase requirements are not explicitly absent')
        prices = [p.get('input_nano_usd_per_million'), p.get('output_nano_usd_per_million')]
        if any(type(value) is not int or value != 0 for value in prices):
            reasons.append('Input/output prices are nonzero or unknown')
    # Current provider schema does not attest ALL billable dimensions and effective
    # org-specific waterfall routes. Do not invent a supported zero-charge flag.
    reasons.append('No authoritative route-complete zero-charge billing assertion')
    return {'status': 'UNVERIFIED', 'reasons': sorted(set(reasons))}


async def get_policy(store):
    row = await store.one('system_settings', {'id': 'model-routing'})
    return {'policy': row['config'], 'updated_at': row['updated_at']} if row else {'policy': RoutingPolicy().model_dump(), 'updated_at': None}


async def save_policy(store, policy, actor):
    value = {'config': policy.model_dump(), 'updated_at': now()}
    await store.db.system_settings.update_one({'id': 'model-routing'}, {'$set': value}, upsert=True)
    await store.audit('master', 'Free-first routing policy saved', 'Role priorities and bounded failure criteria updated; billing proof and budgets unchanged', actor=actor)
    return await get_policy(store)


async def policy_view(store):
    data = await get_policy(store)
    catalog = await store.one('integration_connections', {'id': 'experiential'}) or {}
    ids, free = catalog.get('model_ids', []), current_free_models(catalog)
    groups = {}
    for role in ['routine', 'coding', 'review']:
        preferred = ordered_models(data['policy'][role], ids)
        candidates = ordered_models(data['policy'][role], free)
        groups[role] = {'available_preferences': preferred, 'verified_free_order': candidates + sorted(set(free) - set(candidates))}
    return {**data, 'strategy': 'FREE_FIRST', 'catalog_model_count': len(ids), 'catalog_verified_at': catalog.get('catalog_verified_at'),
            'verified_free_count': len(free), 'groups': groups, 'paid_execution': 'DISABLED',
            'free_status': 'VERIFIED_FREE_AVAILABLE' if free else 'FREE_MODEL_UNVERIFIED',
            'free_reason': ('Fresh server-verified zero-charge routes are available.' if free else 'Current catalog metadata does not prove account-available $0 billing across every route. Estimates, names and BYOK are not proof.'),
            'eligibility': catalog.get('free_eligibility', {}),
            'decisions': await store.rows('routing_decisions', limit=30)}