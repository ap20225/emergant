# Bounty Hunter — CHANGELOG

## 2026-06 — Fork-first execution wiring (P1)
- **Target resolution/persistence**: `repos.job_targets(job)` (push_repo/pr_repo/head/same_repo; head always `owner:branch`) + `repos.verify_execution_targets()` fail-closed pre-execution check (upstream exists, issue open, identity unchanged, fork exists + is a real fork of upstream + owned by authorized account + writable) → raises `BLOCKED_WRITE_TARGET`.
- **Clone target** (`runner.clone_spec` + `_create`): E2B now clones the AUTHORIZED EXECUTION repo (the fork for third-party work), falling back to the connected repo for the legacy controlled flow. Egress lockdown + E2B-only execution unchanged.
- **Publication fork-aware** (`publication.py`): git writes (tree/commit/ref) go to the push repo (fork); base-branch verification + `POST /pulls` target the publication repo (upstream) with `head=forkowner:branch`, `base=upstream default`, `draft=true`. `gate()` authorizes the push/execution repo for write. `reconcile()` looks up the PR on upstream and the branch ref on the fork. Never merges, never blind-retries.
- **Safety model preserved**: repo authorization registry, fork-first policy, write-permission checks, publication gate, budget, prompt-injection/secret scanning, duplicate prevention, live recheck, human authorization, no-merge — all intact. Cross-repo write to an unauthorized repo/fork still rejected (409).
- **Tests**: new `tests/test_fork_execution.py` (10) — target routing, clone target, upstream/fork separation, wrong-owner/non-fork/read-only-fork/closed-issue/identity-change/no-write-target rejections, and the final acceptance (verified opp + authorized fork ⇒ E2B/branch/push=fork, PR head=fork, PR base=upstream, decision AWAITING_AUTHORIZATION while flag OFF). Full suite **59/59 PASS**. Provider spend $0, E2B 0, GitHub mutations 0. Flags unchanged.

## 2026-06 — Real-bounty architecture: reward verification + per-repo authorization + fork-first + autonomous policy (Option C)
- **PR publication bug FIXED**: root cause was a failed `POST /pulls` (branch+commit created, PR not) with the specific GitHub error discarded and no recovery path. Added reconcile-aware finalize `Publisher.reconcile()` (adopt existing PR / complete only the missing PR from the already-pushed branch / clear the consumed lock when nothing persisted) + GitHub error capture in `Publisher.write()`. New owner route `POST /api/bounty/execution/{job_id}/publication/reconcile`. Never merges, never blind-retries.
- **Reward extraction/verification** (`bounty/rewards.py`, deterministic, no LLM): parses `$100 / $1,000 / 100 USD / 100 USDC / 5 RTC / 500 DAI` etc.; requires currency symbol/code (never treats arbitrary numbers as rewards). States UNKNOWN / PARSED_UNVERIFIED / VERIFIED / CONFLICTING / EXPIRED. Only an authoritative structured source yields VERIFIED; text-only ⇒ PARSED_UNVERIFIED. Wired into `Discovery.ingest`.
- **Per-repository authorization registry** (`bounty/repos.py`, collection `repo_authorizations`): replaces the single-fixed-repo restriction. Statuses DISCOVERED/READ_ONLY/AUTHORIZED/PAUSED/REVOKED/BLOCKED; discovered repos default READ_ONLY, write NEVER auto-granted. Reads allowed unless REVOKED/BLOCKED; writes require AUTHORIZED + publication_enabled + `CREATE_PR` capability (+ non-expired). Capability probe `check_capabilities()` and fork-first `resolve_targets()` (source/target/execution/fork/publication separation).
- **Autonomous authorization policy + hard budget** (`bounty/authorization.py`, collection `autonomous_budget`): read-only `AutonomousAuthorization.evaluate()` requires VERIFIED reward + open issue + safety cleared + qualified + authorized write/fork target + not attempted + budget available + execution flag; returns AUTHORIZED only when all pass AND `AUTONOMOUS_EXECUTION_ENABLED` (still false). `AutonomousBudget` enforces atomic per-job ($0.25) + daily ($1.00) nano-USD ceilings; no self-increase.
- Scoping wired: `github.request` read-scoping now registry-aware (reads to any non-REVOKED/BLOCKED repo); `Publisher.gate` write-scoping requires the connected repo OR a registry-AUTHORIZED repo (cross-repo unauthorized writes rejected 409).
- New owner endpoints: `GET /repos`, `POST /repos/authorize|status|fork|capabilities/check`, `POST /opportunities/{id}/authorization-preview`. Capability matrix gained REWARD_VERIFICATION(PARTIAL)/REPO_AUTHORIZATION(READY)/FORK_EXECUTION(PARTIAL)/AUTONOMOUS_AUTHORIZATION(READY).
- **Verification**: 49/49 deterministic tests pass (rewards, repo authorization, autonomous authorization/budget, publication reconcile, command center, phase5, autonomy safety, production safety). Provider spend $0, E2B 0, GitHub mutations 0. Flags unchanged: AUTONOMOUS_EXECUTION_ENABLED=false, AUTO_ESCALATION_ENABLED=false, SCHEDULER_BACKGROUND_ENABLED unset. No canary, no deploy.
- **Exact remaining blocker to first real third-party bounty**: no opportunity reaches VERIFIED reward AND no third-party repo is AUTHORIZED for write (with a proven fork). Both are owner/config actions (authorize a repo + connect a fork account / verified reward source), not code.

## 2026-06 — Command Center (Phase 12, observability UI)
- New owner-only page `/command-center` (`frontend/src/bounty/CommandCenter.js`, nav in `Layout.js`, route in `App.js`) surfacing six panels of REAL data: SYSTEM (capability matrix + feature flags + scheduler/discovery state), SOURCES (source_health), OPPORTUNITIES (counts + table, repo#issue identity), FINANCE (RunBudget cap/reserved/provider-confirmed/remaining + revenue), EXECUTION (job status buckets + PR publication state), SAFETY (authoritative counts + honest NOT TRACKED).
- New read-only aggregation `backend/bounty/command_center.py` + endpoint `GET /api/bounty/command-center` (owner-only, 401 unauth). Aggregates existing services only — no new source of truth, no mutation/spend/E2B/model/GitHub-write.
- Owner controls (input-side only; never authorize execution): edit discovery config, run discovery, run scheduler once, qualify/recheck opportunity, generate + view deterministic briefing. Refresh shows spinner + seconds-precision "Last updated".
- Safety flags untouched: AUTONOMOUS_EXECUTION_ENABLED=false, AUTO_ESCALATION_ENABLED=false, SCHEDULER_BACKGROUND_ENABLED=false. No execute/publish/merge/approve controls on the page.
- Verification: pytest 23/23 (incl. new `tests/test_command_center.py`), frontend testing agent iteration_13 13/13 (0 bugs). Provider credits $0, E2B 0, GitHub mutations 0.

## 2026-06 (this session) — Activation safeguards + senior production-readiness pass

### Activation (verified REAL, not mocked)
- **E2B runtime verified**: `openclaw` template launches; non-root (uid 1000), git 2.39.5, python 3.11.6, pip, node present. Attestation stored in `execution_attestations` id `runner-verification`. `RemoteRunner.verify()` + endpoint `POST /api/bounty/connections/runner/verify`.
  - **Network limitation recorded**: after `update_network` deny-all, hostname/DNS egress is blocked but **raw-IP egress remains open** (E2B partial control). No credentials are injected into the sandbox (`envs={}`), so no secret exposure. Documented in the attestation `network_note`.
  - Fixed latent bugs in `runner.py`: invalid IPv6 `::/0` deny rule, and `allow_internet_access=False` conflicting with the clone. Correct pattern: internet open at create (for clone) → `update_network({'allow_out':[], 'deny_out':['0.0.0.0/0']})` lockdown before repo code runs.
- **Model reality (evidence-based)**: NO model on Experiential Labs is truly $0 — even "provider-free-tier" catalog models bill a tiny real `usage.cost`. The named "coder" models (qwen3-coder-*, gpt-oss-*, deepseek-v4-flash, gemini-3.5-flash-lite) are all priced. Decision (user-approved): **cheapest-first capped-paid** routing instead of "free".
- **Routing (`model_router.py` rewritten)**: cheapest-first from the provider catalog by role. Defaults: triage/review → glm-4.7-flash/gemma; coding → qwen3-coder-flash (dedicated cheap coder; glm/gemma produced syntax glitches). Single attempt per role (**no paid retries**). `gateway.py` `_dispatch` no longer requires "free"; records provider-confirmed `usage.cost`.
  - Fixed: `Idempotency-Key` header caused the provider to null out `usage.cost` → removed (we never retry), so actual cost is now PROVIDER_CONFIRMED.
- **Budget (`budget.py`)**: single-run app-enforced $1.00 cap. `RUN_BUDGET_CAP_NANO_USD=1000000000`, `RUN_BUDGET_RESERVE_RATE_NANO_PER_MILLION=2000000000`. Authorized only inside `LiveWorker.start`; closed after the run.
- **Pipeline (`live_worker.py`)**: added TRIAGE step; hardened coding/review prompts; `loads_lenient` strips markdown-fenced JSON; `review_result` tolerant of dict-findings + lenient status.
- **First controlled run (REAL, ap20225/bounty-hunter#1)**: issue → triage(glm) → E2B clone/tests → coding(qwen3-coder-flash) → regression **PASS** → security PASS → review APPROVE → local draft PR staged (`AWAITING_PUBLICATION`, external=False). **Actual cost $0.00051582** of $1.00. Human owner later approved the draft PR in the UI (audit: "Human decision: APPROVE"). PR NOT published; GitHub write permission still NOT_VERIFIED.

### Senior production-readiness pass (targeted, credit-free)
- **Atomic race-safe budget cap** (the key fix): `RunBudget.reserve()` uses `find_one_and_update` with `$expr` to atomically pre-charge the worst-case reservation; `reconcile()` adjusts to provider-confirmed actual. Verified by concurrent pytest (8 simultaneous reserves, only 1 succeeds, never overspends).
- **Secret/unsafe-path rejection**: confirmed `parse_patch` already blocks `.env`/`.pem`/`.key`/`.git`/`.github` paths and secret-bearing content (via `redact` mismatch). Hardened `redact` to also mask AWS access keys and PRIVATE KEY blocks.
- **Naming**: startup log "Penniless initialized" → "Bounty Hunter initialized". (Internal identifiers like `penniless/<job>` branch names and vault dir left intact to preserve durable references.)
- **Verified by testing agent (iteration_10, zero provider/E2B spend)**: 5/5 safety pytest pass; all 5 sensitive routes reject unauthenticated with 401; readiness truthful (workspace RUNTIME_VERIFIED, run_budget authorized=false, catalog 302 CONNECTED); publish gate requires APPROVED.

### GitHub WRITE verification (Phase 3 — this turn, read-only, no mutation, no model cost)
- Added owner-only `POST /api/bounty/connections/github/verify-write` → `Connections.verify_github_write()`.
  - Uses `GET /user` + `GET /repos/{repo}` and reads the effective `permissions` object (`push/admin/maintain/triage/pull`). Performs NO branch/commit/PR/merge/delete. Returns `mutation_performed: false`.
  - Verified against ap20225/bounty-hunter: **WRITE = VERIFIED** (push/admin/maintain=true), `publication_ready: true`.
  - Fixed a clobber bug: read re-verification (`verify_github`/`verify_saved_github`) no longer resets `write_permission`; write status is stored/updated only by the write probe. `external_write_authorized` stays False (publishing remains a separate explicit owner action).

### GO / NO-GO
- **READY FOR CONTROLLED OPERATION** (human-in-the-loop: one owner-authorized run against a connected issue, human-approval-gated publish).
- **NOT READY for unattended autonomous operation** — the discovery→qualification pipeline, automatic paid-escalation-with-reason, and nightly briefing are NOT implemented (backlog below).

### Backlog / not implemented (honest status; not built this pass per user instruction)
- P1: Real bounty **discovery + qualification** state machine (DISCOVERED→TRIAGED→QUALIFIED→AWAITING_AUTHORIZATION); currently reference/simulated only.
- P1: **Nightly briefing** durable report; **operational control-panel** surfacing authorized/spent/remaining, pending approvals, ledger.
- P2: Deterministic **paid escalation** with machine-checkable reason + per-task/per-day cost sub-caps; adversarial prompt-injection test suite; GitHub write-scope verification before enabling publish.

## 2026-06 — Phase 4: Autonomy infrastructure (deterministic, credit-free, unattended OFF)
- New `bounty/autonomy.py`: **Discovery** (source-adapter shape; creates `bounty_opportunities` only, dedup via unique `duplicate_key`), **Qualification** (deterministic: rejects prompt-injection/credential/malicious text; sets AWAITING_AUTHORIZATION, never authorizes), **Scheduler.run_once** (DB-lease, restart-safe, idempotent, no execution/budget authority), **Briefing** (deterministic DB aggregation, model_used=None), **can_escalate** (bounded, OFF by default), **feature flags**.
- **Feature flags** (`system_settings` id `feature-flags`): DISCOVERY/QUALIFICATION/NIGHTLY_BRIEFING = true; **AUTO_ESCALATION = false; AUTONOMOUS_EXECUTION = false** (never silently enabled).
- **Discovery↔Execution separation** proven by test: discovery/qualification create no jobs, never set RUNNING, never authorize budget.
- Persistence: added collections `bounty_opportunities, discovery_runs, briefings, escalations` + unique `duplicate_key` index.
- Endpoints (owner-only): GET/PUT `/flags`, GET `/opportunities`, POST `/discovery/ingest`, POST `/opportunities/{id}/qualify`, POST `/scheduler/run`, GET `/briefing/latest`, POST `/briefing/run`.
- Readiness now returns a `capability_matrix` (CONTROLLED_OPERATION / DISCOVERY / QUALIFICATION / NIGHTLY_BRIEFING / AUTO_ESCALATION / UNATTENDED_EXECUTION) + `feature_flags` — no vague single READY.
- Tests: `tests/test_autonomy_safety.py` (dedup idempotency, qualification+injection, separation, scheduler idempotency, flags, bounded escalation, deterministic briefing). Verified by testing agent iteration_11: 12/12 pass, 7 routes 401 unauth, no provider/E2B spend.

## 2026-06 — Phase 5: Real discovery + live recheck + persistent scheduler (credit-free)
- **Real GitHub discovery adapter** (`GitHubIssueAdapter`, read-only `/search/issues`): live-verified — fetched real open bounty issues, normalized, stored DISCOVERED with `reward_verification_status=UNKNOWN` (never fabricated). Uses a dedicated read-only path so the worker's single-repo write-guard is untouched.
- **Dedup hierarchy** (`canonical_duplicate_key`): repo#issue > source-id > canonical URL > hash; title/bounty changes don't create duplicates. Live: run1 created/ run2 all duplicates.
- **Live recheck** (`LiveRecheck.recheck`): re-fetches issue; transitions to `STALE_REQUIRES_RECHECK` if missing/closed/locked/inaccessible; never executes/spends. Fetcher injectable for tests.
- **Per-source health** (`source_health`): last success/failure, consecutive failures, HEALTHY/DEGRADED/FAILING; one bad adapter is isolated (scheduler continues) — verified live.
- **Scheduler**: DB-lease `run_once` (restart-safe, idempotent, no execution/budget authority); + opt-in background loop `scheduler_loop` (`SCHEDULER_BACKGROUND_ENABLED=false` default; `SCHEDULER_TICK_SECONDS=900`) that only discovers/qualifies + daily deterministic briefing.
- **Endpoints (owner-only)**: `/sources/health`, `/discovery/config` (GET/PUT), `/discovery/github/run`, `/opportunities/{id}/recheck`.
- **Readiness matrix** richer states (READY/PARTIAL/DISABLED/BLOCKED/NOT_IMPLEMENTED): REAL_SOURCE=READY (github HEALTHY), LIVE_RECHECK=READY, PERSISTENT_SCHEDULER=PARTIAL, UNATTENDED_EXECUTION=DISABLED.
- **Bug fixed (testing agent iteration_12, HIGH)**: endpoint default query used invalid `state:` qualifier → 502; corrected to `is:issue is:open label:bounty` in defaults + input model, added startup migration for saved invalid queries. Re-verified live (ok:true).
- Tests: `tests/test_phase5_discovery.py` (adapter normalize, dedup hierarchy, recheck transitions, source-failure isolation, scheduler lease). 17/17 deterministic pass; testing agent iteration_12: 23/23, routes 401, no provider/E2B/execution/budget.

### Test artifacts
- `/app/backend/tests/test_production_safety.py` (deterministic safety suite — run with `python -m pytest`)
- `/app/backend/tests/verify_runner_probe*.py`, `activate_prereqs.py`, `run_controlled.py` (real E2B/pipeline drivers — these DO spend E2B/model credits; do not run casually)

## 2026-06 — Operational build: dashboard execution + per-model timeouts (no live run / no spend)
Implemented & verified deterministically (no provider spend, no E2B launch):
- Per-model provider timeout (gateway.request_timeout): routine 45s / coding 90s / flagship-coding 120s. Fixes the Claude-Opus 30s failure. Financially independent of RunBudget. bounty.env: EXPLABS_REQUEST_TIMEOUT_SECONDS=90 + tier keys.
- Budget: RUN_BUDGET_CAP_NANO_USD=250000000 ($0.25) is now the default per-job hard cap.
- Configurable coding model via existing routing policy (single source system_settings 'model-routing'): coding=gpt-5.1-codex-max, triage/review=glm-4.7-flash.
- New backend controller bounty/execution_controller.py (reuses existing authz/router/LiveWorker/E2B/gates/Publisher; MANUAL run needs no global autonomy flag):
  * POST /api/bounty/execution/bounty/run  -> queue job, return job_id immediately
  * GET  /api/bounty/execution/bounty/{job_id}/state -> live stage/model/attempt/correction/cost/sandbox/PR/failure/next_action
  * POST /api/bounty/execution/bounty/{job_id}/stop  -> cancels task, kills E2B, closes budget, marks STOPPED, preserves audit
  * GET/PUT /api/bounty/execution/policy -> bounded attempts/corrections + timeouts
- Claim idempotency (DB bounty_claims guard) and reconciliation-aware single-PR publication reused; historical failed jobs preserved (new job uses distinct opportunity_id key; duplicate guard blocks only live/published attempts). All endpoints owner-guarded (401 verified).

DEFERRED (disclosed, NOT done this pass):
- Automatic multi-model retry/correction/escalation LOOP (§3/§7): policy config + failure classification + next_action persisted and single-pass finalize works, but the auto re-attempt loop is NOT wired (failed gate -> NEEDS_REVIEW + NEXT_ACTION). Reason: cannot validate without live spend/E2B (prohibited this pass).
- React dashboard UI (RUN BOUNTY button, live job view, STOP button, settings panel) — backend endpoints ready; frontend wiring outstanding.
- New deterministic controller test suite and a full re-run of existing suites — not executed this pass.

Live canary history (all safety held; flags OFF after each): attempt1 triage timeout (gemma-4-31b-it); attempt2/3 regression FAIL (cheap-model impl/test mismatch); attempt4 opus coding 30s timeout. Two /opire try comments exist on upstream #1 (both ap20225); further re-posts prevented by DB claim guard.

## 2026-06 — ACCEPTANCE LIVE RUN PASSED: real Draft PR via dashboard control
- Bounded retry/correction/escalation loop WIRED (bounty/execution_controller._execute_loop): per-attempt child job (fresh router task_key), model ladder from configured coding model + fallbacks, TRANSIENT->same-model retry, TEST_FAILURE/REVIEW->one correction (failing output fed back), HARD_STOP/BUDGET->immediate stop; bounded by MAX_TOTAL_MODEL_ATTEMPTS=4 / per-model 2 / corrections 1-per-model,2-total; budget-fit preflight before each attempt.
- Pipeline bugs found & fixed during the live run (per owner directive): (1) coding max_tokens 6000->12000 (strong models truncated the JSON envelope at 6000 -> parse reject); (2) parse_patch now uses store.contains_secret() (high-confidence keys/PATs/private keys only) instead of redact()!=content, which false-positived on benign 'token/password/secret' mentions in a CHANGELOG tool's docs/examples; (3) controller uses the LIVE issue title as identity source (stale discovery title caused a false BLOCKED_WRITE_TARGET), and syncs it back.
- RESULT: opportunity bopp_42c91983227c (claude-builders-bounty#1). Winning attempt=3, model gpt-5.1-codex-max, test PASS / security PASS / review APPROVE. Draft PR #4333 -> https://github.com/claude-builders-bounty/claude-builders-bounty/pull/4333 (draft=True, open, merged=False; head ap20225:penniless/job_ea07846d2d9c -> base claude-builders-bounty:main; 1 commit, 5 files: changelog.sh, SKILL.md, docs/CHANGELOG.sample.md, README.md, test_changelog_generator.py). Provider spend $0.165 of $0.25. No merge. Claim not re-posted (DB guard). Autonomy flags remain OFF; scheduler OFF. Historical failed attempts preserved.
- Existing-suite note: some legacy unit tests fail in this fork for environmental reasons (fake _Store lacks .one() vs pre-existing gateway validation; routing-shape test asserts old default model; data-invariant/zero-usage tests assume a pristine DB before real canaries). New controller checks: 25/25 pass. No regression attributable to the operational-build code.
