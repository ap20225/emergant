# Real execution pipeline: implementation and remaining activation gates

Updated 2026-09-15. **REAL EXECUTION: NOT READY**. This is the existing Penniless app, not a replacement.

## Current correction and name — 2026-09-16

The visible app is now **Bounty Hunter**. Internal paths/keys/history retain existing identifiers; no secret migration is needed.

**The owner already completed GitHub/E2B configuration in another app window.** Earlier instructions in this historical document saying they are missing must not be repeated. Current saved state, independently verified in iteration9:
- GitHub CONNECTED: `ap20225/bounty-hunter`, issue1, repository and issue reads verified; write permissions not verified.
- E2B credential saved, template `openclaw`, CONFIGURED_UNVERIFIED. No live sandbox/runtime verification or spending authorization.
- Experiential catalog302, no inference/model usage, no verified free billing route or enforceable diagnostic ceiling.

Remaining work is runner/billing activation and safe real verification—not credential re-entry. No GITHUB_REQUIRED/RUNNER_REQUIRED current blockers. Do not override budgets or claim a working worker from saved metadata alone. Reuse the existing configured issue for the first controlled proof once remaining gates are resolved.

The rename/check task made only read-only metadata requests, changed frontend copy/title/saved-runner display, and preserved credentials/accounts/history. Report `/app/test_reports/iteration_9.json`:6 API checks plus owner/public browser checks passed. No mocks/provider calls, inference, GitHub mutation or sandbox launch occurred.

## Update — free-first policy, 2026-09-16

Settings → Model routing now stores ordered routine/coding/review priorities and bounded failure criteria in MongoDB. It applies immediately after Save routing policy. The previous instruction to choose a legacy primary model is superseded; legacy primary_model values no longer determine gateway routing. Security links directly to the new editor.

Fresh authenticated catalog:302 models; requested routine/coding IDs exist, but0 qualify as VERIFIED_FREE. Provider catalog estimates and available names do not attest account-available $0 charges across every effective fallback route and billing dimension. Eligibility remains FREE_MODEL_UNVERIFIED until provider-supported authoritative proof can actually be integrated. No fake zero-charge assertion or editable proof override was added.

At most2 distinct free attempts per task/role; changing policy cannot reset persisted attempts. Explicit failure reason/new approach is required for a later attempt. Security/policy failures, repeated output, failed/ambiguous requests and unconfirmed/nonzero billing prevent further attempts. Paid candidates are logged only after defined failures and remain disabled. The one-time $1 diagnostic allowance is never recurring funding.

The gateway now checks a fresh verified-free routing decision even in its internal dispatch method. Historical tests calling unguarded `_dispatch` directly must expect409 rather than restoring unsafe behavior. Routing decisions preserve model, estimate, token counts (unknown until returned), reason and result; no inference was executed during this change.

Verification `/app/test_reports/iteration_8.json`:15 targeted tests and real owner browser save/reload/restore/navigation/responsive checks passed. Positive provider-proof routing is MOCKED only in isolated tests. No paid/free model calls, new jobs/payments/PRs, or auth changes. GitHub/E2B setup, billing-proof/ceiling integration and first controlled real run remain prerequisites; policy implementation alone does not make the worker operational.

## What was implemented

- Owner-only Security connection screen and API. GitHub Connect verifies identity, an exact user-owned active public repository, and one open issue using GET requests only. No verification issue is created. Read access is not advertised as write access. A different repository requires disconnecting the existing one.
- Encrypted GitHub/E2B server-only credential vault under PENNILESS_SECRETS_DIR, outside Git. Fernet master file0600, directory0700; Mongo holds metadata only. Password fields clear after submitted requests. Validation responses suppress original input values; no keys in responses, prompts, logs or browser storage.
- Actual Experiential HTTP catalog and chat-completion transport, role selection from authenticated catalog, explicit nano-/micro-USD cost parsing, model/tokens/latency/request audit records, no automatic retries/fallback. Unknown billing stays pending; pending billing blocks available funds. Actual charges remain distinct from pricing estimates.
- **Live no-cost check performed:** authenticated /v1/models returned200 and **302** models. Current catalog is persisted. Previous318 was historical, not current. No inference call was sent. No usage record, inference tokens, inference latency or actual inference cost exists; the $1 allowance was not consumed.
- E2B2.29 remote-only adapter: trusted no-checkout public clone, then deny-all network before checkout/branch/tests, nonroot commands, hooks disabled, no application/GitHub/model keys in sandbox environment, fixed hard lifetime and command bounds, cleanup in finally. No host subprocess fallback. Dependencies must already be present in an approved template.
- General real-issue worker stages: bounded pinned-commit inspection → isolated clone/branch → structured model patch → real test-command results → independent security/code model reviews → isolated commit → unpublished artifact and human approval. Protected paths, symlink parents, secrets, empty/oversized patches and modified artifacts fail closed. No automatic paid fix loop.
- Separate human-confirmed publication path: approved artifact hash + tests/reviews + current repository and base commit + policy gates → Git tree/commit/new branch/draft PR. Durable unique publication attempt prevents repeats after ambiguous outcomes. No force push, default-branch updates or merges. Read-only PR synchronization never recognizes payment.
- Dashboard readiness now uses capability/blocker state and displays **Real execution blocked**, not global-stop-only System ready. Security exposes connection/catalog/inference/runner/cost blockers separately. Local drafts, simulated ledgers and real PRs use distinct labels.
- Fixed current owner-browser CSRF false403 by replacing stale fork alias with exactly the observed current ingress origin in BOUNTY_ALLOWED_ORIGINS; no wildcard/suffix/dynamic forwarded-header trust. Existing owner auth and cookies unchanged.

## Important limits — not just missing keys

**The production paths are implemented adapters, not live-verified services.** Current safety gates intentionally remain closed:

1. No GitHub credential/repository/real issue is configured yet.
2. No suitable runner existed in the application environment: no Docker/Podman/Kubernetes access, bwrap or nsjail. E2B key/template not supplied. No E2B sandbox has been created or tested live.
3. A technically enforceable Experiential <=$1 diagnostic ceiling and single-route billing contract are not established. Public catalog pricing is insufficient. `/connections/model/diagnostic` always returns the explicit cap blocker under the current contract; no request identity/reservation is consumed by a rejected check.
4. Ordinary model and runner spending is still $0. The diagnostic authorization is not recurring job funding. `ModelRouter.authorize` and `RemoteRunner.preflight` intentionally reject even after credential configuration if paid capability/ceiling/funding are not established. Provider-specific enforceable ceiling verification and funded activation/reservation logic still need follow-up implementation against a confirmed provider contract. **Adding keys or changing a budget field alone will not make execution run.** Do not manually delete guards to get past this.
5. No real end-to-end issue → patch → tests/reviews → draft PR proof exists. Contract tests with mocks are not such proof. No execution attestation has been created.
6. Payment provider/chain receipt verification remains unconnected and out of this execution phase. Coinbase excluded. No wallet operations.

## Owner setup — next actions in order

1. Create/select a small **public disposable GitHub repository you own** and an open issue with real tests. In Penniless open Security → GitHub. Enter `owner/repository`, the issue number and a fine-grained token **only in the password form**, never in chat. Token setup: GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens. Select only that repo. Begin with metadata/contents/issues read access. Current first-repo policy requires the token identity to own the repo, not just collaborate in an organization.
2. For isolation, configure an E2B account and dependency-ready sandbox template (Git, selected test runtime, non-root `user`, no embedded secrets, approved CPU/memory). Enter its key/template in Security → Isolated runner. Configuration itself creates no sandbox and does not authorize charges. Relevant SDK docs: https://docs.e2b.dev/sdk-reference/python-sdk/v2.29.0/sandbox_async .
3. In Security → Experiential Labs select a primary model from the authenticated catalog; role overrides are optional. Obtain a provider-supported enforceable diagnostic cap/single billing route before the one allowed inference. Separately authorize a bounded real-test model/runner budget if desired; implementation must first wire and verify corresponding reservations/ceiling enforcement. Existing funds cannot be increased automatically, and simulated revenue never counts.
4. Only after the preceding gates are operational, use Security → Controlled real issue with the repository's actual test suite. Review patch/test/security/review evidence in Active Jobs. Approve artifact, then separately confirm Publish approved draft PR. Later write permissions must be deliberately added for Contents and Pull requests; issue write/merge/workflow/admin permission is unnecessary for this publication path. A tiny issue with a failing regression test is the recommended first proof.
5. Only after that proof consider a real paid bounty and its separate payment/receipt verification requirements.

## Verification completed (no fabricated live results)

- Frontend production build and backend compile/ruff critical-error checks: passed.
- Iteration5: 37 backend guard/cost/parser tests passed; public readiness/responsive checks passed. Owner-browser gap subsequently closed.
- Iteration6: 11 additional GitHub/vault/publication/runner/worker/invariant tests passed. **Provider/SDK and worker happy paths were MOCKED in isolated tests only.** Found actual browser origin403; fixed afterward.
- Iteration7: 8 real API origin/auth/gate tests passed. **UNMOCKED** owner browser verified /execution/status200, real302-model catalog state, invalid GitHub input422 with cleared password/no echo, real diagnostic409 with zero usage, no overflow320/1440. Prior owner responsive320/768/1024/1440 checks passed.
- Final testing report: `/app/test_reports/iteration_7.json`; owner browser evidence `/app/test_reports/iteration7_browser/owner_browser_results.json`.
- Existing records preserved: 2 simulated jobs, 3 simulated payments, 0 external PRs, 0 model usage, $0 live verified revenue. Only genuine catalog/blocked-check audit events added. No new owner account/session, no GitHub branch/commit/PR, no arbitrary repository code on application host, no paid model/sandbox, no transfers.

## New/changed code references

Backend bounty modules: `secrets.py`, `connections.py`, `gateway.py`, `model_router.py`, `runner.py`, `patches.py`, `live_worker.py`, `publication.py`, `readiness.py`, `execution_routes.py`; existing `routes.py`, `service.py`, `finance.py`, `github.py`, `store.py`, `orchestrator.py` updated additively.

Frontend: `Connections.js`, `LiveJobActions.js`, `Dashboard.js`, `Jobs.js`, `Operations.js`, `api.js`, `command.css`. Existing Security route hosts the connection interface; existing settings/job/finance architecture retained.

Nonsecret configuration: `backend/bounty.env`; dependencies updated through pip (e2b2.29, cryptography, Playwright for tests). Backend supervisor has no autoreload in this environment; restart after env/dependency updates was used, without changing ports.

## Follow-up priorities

P0: user-owned GitHub proof credentials/repo; runner configuration; provider-supported enforced ceilings and explicitly funded activation/reservation implementation; first real controlled execution/publication verification.

P1: external PR reconciliation for ambiguous publication, revision workflow and read-only real receipt verification. P2: paid opportunity expansion/learning only after real proof. No unrelated dashboard features requested.