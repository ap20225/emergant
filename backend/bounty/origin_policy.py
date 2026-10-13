"""Shared request-origin allowlist for /api/bounty mutating requests and CORS.

Config-driven via BOUNTY_ALLOWED_ORIGINS (comma-separated exact origins). Exact origins feed
BOTH the mutating-request guard and the CORS allow-list, so the deployed PRODUCTION origin is
supported purely through environment configuration — set BOUNTY_ALLOWED_ORIGINS to the live
origin (e.g. https://<app>.emergent.host) at deploy time and owner write actions work from it.

In addition to exact matches, it accepts the SIBLING Emergent host-forms of the SAME app slug —
the preview canonical ``<slug>.preview.emergentagent.com`` / cluster alias
``<slug>.cluster-<n>.preview.emergentcf.cloud`` and the PRODUCTION forms ``<slug>.emergent.host``
/ ``<slug>.emergent.cloud`` are forms of one app. A preview cluster change or the preview→prod
transition therefore does not require re-pinning when the slug is unchanged, while still never
allowing arbitrary origins (unknown slug or non-Emergent host is rejected). No wildcard is used.
"""
import os
import re
from urllib.parse import urlparse

PREVIEW_SUFFIXES = ('.preview.emergentagent.com', '.preview.emergentcf.cloud')
PRODUCTION_SUFFIXES = ('.emergent.host', '.emergent.cloud')
EMERGENT_SUFFIXES = PREVIEW_SUFFIXES + PRODUCTION_SUFFIXES


def configured_origins():
    return [v.strip().rstrip('/') for v in os.environ.get('BOUNTY_ALLOWED_ORIGINS', '').split(',') if v.strip()]


def emergent_slug(host):
    """Leading app slug of an Emergent preview/production host, else None (non-Emergent host)."""
    if not host:
        return None
    for suffix in EMERGENT_SUFFIXES:
        if host.endswith(suffix):
            return host[:-len(suffix)].split('.')[0] or None
    return None


def _configured_slugs(origins):
    return {s for s in (emergent_slug(urlparse(o).hostname or '') for o in origins) if s}


def origin_allowed(origin, origins=None):
    """True if the browser Origin is explicitly configured, or an Emergent preview/production
    host-form sharing a configured app slug. A missing Origin returns True so native/server
    clients and same-origin requests keep working — browsers always send Origin on cross-origin
    mutating fetches, so this does not weaken CSRF protection."""
    if not origin:
        return True
    origins = configured_origins() if origins is None else origins
    if origin in origins:
        return True
    parsed = urlparse(origin)
    if parsed.scheme != 'https':
        return False
    slug = emergent_slug(parsed.hostname or '')
    if not slug:
        return False
    return slug in _configured_slugs(origins)


def cors_origin_regex(origins=None):
    """Starlette CORS regex covering the configured Emergent slugs in every host-form (preview
    canonical/cluster alias and production .emergent.host/.emergent.cloud). Returns None when no
    Emergent slug is configured (exact production origins are still covered by allow_origins)."""
    origins = configured_origins() if origins is None else origins
    slugs = sorted(_configured_slugs(origins))
    if not slugs:
        return None
    group = '|'.join(re.escape(s) for s in slugs)
    return (r'^https://(' + group + r')'
            r'((\.cluster-\d+)?\.preview\.emergent(agent\.com|cf\.cloud)|\.emergent\.(host|cloud))$')
