# Experiential migration status — 2026-09-15

**User stopped work with `FINISH`. Migration is incomplete. Do not claim gateway agents are connected.**

## Requested final-report fields
1. Files changed: backend/bounty.env (non-secret configuration); memory/PRD.md; memory/test_credentials.md; this handoff; no-cost catalog inspection reports under test_reports. Secret file lives outside Git at /root/.config/penniless/.env and must never be printed.
2. Gateway configuration: documented inference base https://api.experientiallabs.ai/v1 and management base https://api.experientiallabs.ai/api. Secret stored securely. Existing ModelRouter adapter and server secret loading have NOT been replaced/wired.
3. Models discovered: actual authenticated GET /v1/models200,318 IDs. First enriched /api/models page200,400 of908 rows. Intersect by model.slug with granted IDs; do not use public rows as authorization.
4. Default model per agent: none selected/changed. Existing model routing fields remain NOT CONFIGURED.
5. Cost controls created: none beyond existing zero-budget guards. Proposed nano-dollar reservations/settlement not implemented.
6. Budget controls created: none. User explicitly authorized one diagnostic up to$1, no paid retry or automatic increases. This allowance is unused and must not silently increase normal agent/operating budgets.
7. Usage tracking implemented: not yet. No inferred/hallucinated usage or charge was recorded.
8. Test model: none; no inference dispatch.
9. Test result: no-cost authentication passed. Minimal inference and full migration regressions NOT RUN. Existing reference-platform verification remains documented in PRD.
10. Remaining configuration/work: adapter implementation, secret loading, full paginated granted-model registry, org discovery, usage correlation, atomic spending controls, per-role model settings, cache, controlled fallback, command answers, tests, and only then the single authorized diagnostic if user still wants to proceed.

## Verified contracts and observations
- Integration playbook was consulted twice and official docs retrieved.
- /v1/models currently returns {object, data:[{id,object,created,owned_by}]} with no pricing extension in the observed response.
- /api/models returns {models:[{model,providers,default_provider_ids,...}],total,limit,offset,...}. Provider rates: input_nano_usd_per_million, output_nano_usd_per_million, possibly reasoning/long-context variants. Pricing source estimate is display-only, not safe for budgeted execution.
- Providers distinguish host_managed and customer_managed. Capabilities include chat_max_tokens_field, supports_tools/reasoning, maximum_output_tokens. Model max_output_tokens can be null even if provider capability supplies a bound. Missing metadata is unknown, not zero/false.
- /api/keys returns id/name/key_prefix/key_suffix/expiry metadata, NO org_id. Never forward the key inventory to the frontend.
- /api/gateway/catalog without org_id422. Latest playbook identifies /api/whoami for org_id discovery; not independently tested yet. If unavailable, request dashboard org_id rather than guessing.
- Usage APIs are /api/gateway/usage/events and /api/gateway/usage/daily with org_id. Current official billing docs use cost_nano_usd (hosted), estimated_cost_nano_usd (BYOK), spend_nano_usd (rollups). Older micro-USD examples in first playbook are stale.
- Match actual usage only by documented gateway/request identifiers. Do not infer per-request actual charges from aggregate rollups or catalog prices. Missing actual cost remains pending/unknown.
- Send a unique Idempotency-Key and bounded output tokens. Never retry the authorized diagnostic, including on timeout. Preserve ambiguous-cost reservations until reconciled.
- Provider-side waterfall may internally fail over. Local selection must reserve a conservative price envelope; never alter provider waterfalls, credits overflow, key limits, account routing or recharge settings automatically.
- Normal budgets remain0. Proposed diagnostic should be a durable, single-use, explicitly authorized scope—not a fake bounty payment or an automatic operating-budget increase.

## Relevant existing files
- backend/bounty/model_router.py: current stub and abstraction to preserve.
- backend/bounty/store.py, finance.py, routes.py, service.py, models.py: existing DB, finance, owner API and settings.
- frontend/src/bounty/Settings.js, Finance.js, Layout.js, Operations.js: existing UI to extend without redesign.
- Backend supervisor has no --reload; see PRD before verifying runtime edits. Do not change ports or create another server.
- Existing Google owner auth must remain unchanged. Secret configuration is server-only; never put EXPLABS_API_KEY in a frontend-prefixed env variable.

## No-cost evidence
- /app/test_reports/explabs_catalog_auth.json: authenticated model IDs (no secrets).
- /app/test_reports/explabs_model_metadata.json: first enriched metadata page (no full secrets).
- No inference response, paid cost record, model usage receipt or registered role default exists.