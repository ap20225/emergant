"""Stub / synthetic / farmed bounty-repository guard (READ-ONLY, discovery-side only).

Detects high-confidence synthetic, farmed or cloned bounty repositories before an opportunity is
RECOMMENDED for execution, using MULTIPLE signals (never small-size alone):

  - a personal account hosting a repo whose name squats a well-known project ("traefik", "pgx", ...)
  - a fork of a famous project presented as its own bounty target, from a low-traction account
  - self-filed, templated bounty issues on a brand-new zero-traction repo with a single contributor

HIGH confidence  -> excluded from recommendations.
MEDIUM / LOW     -> allowed, only ranked lower and flagged.
NONE             -> clean / established (orgs, stars, real history) — never penalised for being small.

This is a repository-QUALITY heuristic only. It does NOT change payout verification or execution
authorization rules, and it never rejects a legitimate small repository just because it is small.
"""
from datetime import datetime, timezone

from fastapi import HTTPException

# Well-known project / package names commonly squatted by farmed bounty repos.
FAMOUS_NAMES = {
    'react', 'vue', 'angular', 'svelte', 'next', 'nextjs', 'nuxt', 'django', 'flask', 'fastapi',
    'rails', 'laravel', 'express', 'expressjs', 'spring', 'springboot', 'tensorflow', 'pytorch',
    'numpy', 'pandas', 'scipy', 'kubernetes', 'k8s', 'docker', 'moby', 'traefik', 'pgx', 'godot',
    'deno', 'node', 'nodejs', 'typeorm', 'strapi', 'electron', 'keycloak', 'octocrab', 'redis',
    'postgres', 'postgresql', 'mongodb', 'mongo', 'webpack', 'vite', 'rollup', 'babel', 'eslint',
    'prettier', 'graphql', 'apollo', 'nestjs', 'gatsby', 'tailwind', 'tailwindcss', 'bootstrap',
    'jquery', 'lodash', 'axios', 'moment', 'rxjs', 'kafka', 'spark', 'airflow', 'grafana',
    'prometheus', 'terraform', 'ansible', 'helm', 'envoy', 'istio', 'consul', 'vault', 'nginx',
    'llama', 'langchain', 'transformers', 'pydantic', 'sqlalchemy', 'celery', 'gin', 'echo',
    'actix', 'tokio', 'serde', 'rocket', 'bevy', 'wasm', 'flutter', 'ionic', 'nestjs',
}


def _age_days(iso):
    try:
        dt = datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


def _looks_templated(body):
    if not body:
        return False
    b = body.lower()
    hits = 0
    for marker in ('## description', '## requirements', '## acceptance', 'acceptance criteria',
                   'deliverable', 'reward:', 'bounty:', 'payout', 'complete the following',
                   'implement the feature', 'todo:', '- [ ]'):
        if marker in b:
            hits += 1
    # very short + generic, or heavily-templated checklist body
    return hits >= 3 or (len(body) < 140 and ('bounty' in b or 'reward' in b or 'implement' in b))


async def _contributor_count(github, repo, cap=2):
    try:
        rows = await github.request('GET', f'/repos/{repo}/contributors?per_page={cap}&anon=true')
        return len(rows) if isinstance(rows, list) else None
    except HTTPException:
        return None
    except Exception:
        return None


async def assess_repo_quality(github, repo, repo_meta=None, issue=None):
    """Return {'confidence','synthetic','score','signals'}. HIGH => exclude from recommendation."""
    out = {'confidence': 'NONE', 'synthetic': False, 'score': 0, 'signals': []}
    if not repo or not repo_meta:
        return out
    owner = str(repo).split('/')[0].lower()
    name = str(repo).split('/')[-1].lower()
    owner_type = (repo_meta.get('owner') or {}).get('type', '')
    is_org = owner_type == 'Organization'
    is_fork = bool(repo_meta.get('fork'))
    stars = repo_meta.get('stargazers_count') or 0
    forks = repo_meta.get('forks_count') or 0
    subs = repo_meta.get('subscribers_count') or 0
    created = _age_days(repo_meta.get('created_at'))
    pushed = _age_days(repo_meta.get('pushed_at'))
    sig = out['signals']

    # Established / trusted -> never flagged (small size is NOT a signal).
    if stars >= 25 or (is_org and stars >= 5):
        return out

    name_squat = name in FAMOUS_NAMES and owner not in FAMOUS_NAMES and not is_org and stars <= 2
    fork_of_famous = is_fork and name in FAMOUS_NAMES and stars <= 2
    brand_new = created is not None and created <= 60
    no_traction = stars <= 1 and forks <= 1 and subs <= 2
    stale_at_birth = (created is not None and pushed is not None and abs(created - pushed) <= 1)

    templated = False
    self_filed = False
    if issue:
        iu = (issue.get('user') or {}).get('login', '').lower()
        self_filed = bool(iu and iu == owner)
        templated = _looks_templated(issue.get('body'))

    score = 0
    if name_squat:
        score += 6; sig.append('NAME_SQUATS_FAMOUS_PROJECT')
    if fork_of_famous:
        score += 6; sig.append('FORK_OF_FAMOUS_AS_BOUNTY')
    if brand_new:
        score += 1; sig.append('BRAND_NEW_REPO')
    if no_traction:
        score += 1; sig.append('NO_TRACTION')
    if stale_at_birth:
        score += 1; sig.append('NO_DEVELOPMENT_HISTORY')
    if self_filed and templated:
        score += 2; sig.append('SELF_FILED_TEMPLATED_ISSUE')
    elif templated:
        score += 1; sig.append('TEMPLATED_ISSUE')

    # A single contributor only matters alongside another farming signal (avoid punishing solo devs).
    if score >= 1 and not is_org:
        contribs = await _contributor_count(github, repo)
        if contribs is not None and contribs <= 1:
            score += 1; sig.append('SINGLE_CONTRIBUTOR')

    if name_squat or fork_of_famous or score >= 6:
        confidence = 'HIGH'
    elif score >= 3:
        confidence = 'MEDIUM'
    elif score >= 1:
        confidence = 'LOW'
    else:
        confidence = 'NONE'
    # Organizations are never hard-excluded by this heuristic (cap at LOW).
    if is_org and confidence in ('HIGH', 'MEDIUM'):
        confidence = 'LOW'
    out.update(confidence=confidence, synthetic=confidence == 'HIGH', score=score)
    return out
