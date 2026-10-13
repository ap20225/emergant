# Penniless migration — 2026-09-15

## Inspected baseline (not assumptions)
- Existing project: React 19 / CRA + CRACO, FastAPI, Motor/MongoDB, supervisor-managed OpenClaw.
- Existing identity: Emergent-managed Google login, MongoDB sessions, an existing instance owner. Preserved.
- Database collections before migration: users, user_sessions, instance_config, moltbot_configs. No dropshipping tables/collections found.
- Existing agent: one generic OpenClaw main agent, no configured specializations. Empty cron schedule; commented-out heartbeat. No dropshipping prompts, shop/catalog workflows, or channel integrations found.
- Existing provider configuration: Emergent LLM proxy with OpenAI/Anthropic models. Preserved on disk; not invoked by bounty workers.
- Useful infrastructure: UI components, sonner, router, FastAPI auth dependencies, MongoDB client, supervisor helpers, logs.

## Migration decisions
1. No blind deletion. Legacy Python helpers, setup UI file, gateway files and OpenClaw workspace remain intact.
2. Existing users, sessions and instance ownership remain unchanged.
3. Legacy gateway startup, proxy endpoints and unrelated WhatsApp auto-fix watcher are deactivated via BOUNTY_LEGACY_AUTOMATION_ENABLED=false. Gateway stopped; gateway.env not edited.
4. Bounty modules attached to the same FastAPI app and MongoDB database. New routes use /api/bounty and the existing owner dependency.
5. UI default is now the Penniless command center. Unauthenticated visitors see only a read-only zero-data shell; real operational data and actions require the existing owner.
6. New collections and indexes are additive and idempotent.
7. Reference workspaces persist at /app/jobs/{job_id}/{repository,artifacts,logs,tests,reports,runtime}. Excluded from project Git tracking. Logical job layout preserved; changed from ephemeral /jobs following persistence reminder.

## Phase 1 safety boundary — explicit limitations
- Bundled Python reference fixture only: actual local Git branch, reproducible failing tests, actual one-line patch, regression execution, static security and independent deterministic code review, commit, local PR Markdown artifact, human decision.
- Reference execution uses chroot + dropped uid/gid + seccomp network/process/privilege syscall denials + CPU/memory/file/fd limits + clean environment. No host files or secrets are mounted. Not claimed as a production-grade arbitrary-code sandbox.
- Production sandbox and metered general-purpose AI executor are NOT CONNECTED. Arbitrary external repository execution is blocked, even when source/repository discovery policies are enabled.
- Public GitHub issue/repository read APIs are implemented. Credentialed reads and write transport methods are implemented, but end-to-end remote branch/commit/PR publication is disabled pending a properly authorized production execution path and credentials. No actual GitHub PR is claimed.
- Signed GitHub webhook processing and delivery deduplication are implemented but require GITHUB_WEBHOOK_SECRET.
- Safe-test opportunities/rewards/outcomes/financials are explicitly SIMULATED. Only the reference code change/tests/diff/commit are real. They do not count toward live revenue, capital, scoring history, or bootstrap levels.
- Model routes and budget preflight exist; paid inference always fails closed pending a metered provider. No funds are spent.
- Financial entries are stored in integer cents. Live revenue requires payment evidence verification; manual verification is labeled OWNER_ATTESTED_EVIDENCE, not provider-confirmed. No automatic payment integration exists.
- Reference worker escalates unexpected source/test failure instead of repeatedly applying identical patches. General model-driven retry/fix loops remain deferred.
- Autonomous submission and automatic learning-based score updates are not enabled.

## Configuration
- Preserve backend/.env MONGO_URL, DB_NAME and existing provider keys.
- Non-secret execution/configuration values in backend/bounty.env. No hardcoded operational URLs in new adapters.
- Optional backend/.env GITHUB_TOKEN: fine-grained, selected repositories only. GITHUB_WEBHOOK_SECRET: configured alongside repository webhook. Neither is requested or exposed in UI.
- BOUNTY_ALLOWED_ORIGINS matches current preview URL and rejects cross-origin mutations. Update alongside frontend REACT_APP_BACKEND_URL when origin changes.
- The ingress rewrites browser Origin to the exact internal cluster hostname. Both observed first-party origins are explicitly listed in bounty.env; no wildcard is used. An unrelated Origin must still receive403. Forwarded-host headers alone are never trusted as authorization.
- Authentication is Google-based. No new passwords or accounts required. Testing reuses an existing active owner session; credentials document is excluded from source control.