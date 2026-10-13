# Penniless real-execution capability diagnostic

Date: 2026-09-15. Decision: **REAL EXECUTION: NOT READY**.

## Authorized scope and method

The user authorized inspection of configuration, existing records, application code, read-only API/browser checks, and safe local checks. No repository URL was supplied. No paid/provider calls, new branches, commits, PRs, transfers, account changes, or Coinbase work were permitted. This was diagnosis, not an integration implementation or application rebuild.

Independent verification: `test_reports/iteration_4.json`, `iteration4_readonly_evidence.json`, `iteration4_git_evidence.json`, and `pytest/pytest_results_iteration4_readonly.xml`. Five diagnostic tests passed. This means the diagnostic assertions passed, NOT that a real bounty pipeline passed.

Authenticated owner API reads and browser checks were performed using an existing session, without creating credentials. The existing trusted fixture test suites were executed in their reference sandboxes. No new workspace was created because the current workspace creator also initializes Git and commits. The test harness checks that seven tests execute; it accepts exit codes 0 or 1, so this report does not infer a new seven-of-seven pass claim from that assertion.

Before/after application records were unchanged: 2 opportunities, 2 jobs, 2 local PR artifacts, 3 payments, 1 expense, 97 audit records, 0 webhooks. Both jobs and all three payments are SIMULATED. There are zero external PRs and zero live payments. Authenticated live finance returns $0 verified revenue. Existing fixture repositories have no Git remotes.

## Status definitions

- **CONNECTED**: a current service connection was demonstrated; does not imply execution permission.
- **WORKING**: the stated, limited operation was actually exercised or supported by inspected existing evidence; distinguish those below.
- **TEST ONLY**: restricted to the bundled local fixture or simulated business outcome; not an external bounty.
- **NOT CONNECTED**: required credential or service integration is absent.
- **BLOCKED**: unavailable, deliberately disabled, or cannot be demonstrated without missing authorization/infrastructure.

## Capability matrix

| Capability | Status | Evidence and limitation |
| --- | --- | --- |
| GitHub authentication | NOT CONNECTED | No GITHUB_TOKEN in inspected configuration; running owner bootstrap reports NOT CONNECTED. No authenticated GitHub identity demonstrated. |
| Read authorized repositories | BLOCKED | No authorized external repository identified, only bundled local fixture. No credentialed repository read performed. |
| Read real GitHub issues | BLOCKED / unverified | Public-read adapter exists, but no real issue read was exercised during this diagnostic and no real imported opportunities exist. |
| Create isolated workspace | TEST ONLY | Existing per-job fixture workspaces inspected; their test launcher runs. Arbitrary-repository production isolation is NOT CONNECTED. Creation not rerun because it commits. |
| Clone real authorized repository | BLOCKED | Workspace.create writes fixture files rather than cloning; no wired external clone path. |
| Create branch | TEST ONLY | Existing local bounty branches observed using read-only Git commands. No branches created during diagnosis; no live branch workflow. |
| Modify code | TEST ONLY | Bundled deterministic slugify replacement, not a general AI coding worker. Historical local fix commits inspected; no code edited during diagnosis. |
| Run repository's real tests | TEST ONLY | Existing bundled seven-case test suites execute. No external repository test/build/dependency run demonstrated. |
| Security review | TEST ONLY | Deterministic reference-only AST/secret-pattern/file-scope review and existing records. Not a general security audit. |
| Code review | TEST ONLY | Reference-specific function signature, scope and test-evidence checks. General maintainer/AI review is not connected. |
| Commit changes | TEST ONLY | Existing local commits inspected; no new commits made and no remote commit workflow active. |
| Create real draft PR | BLOCKED | Publish route unconditionally refuses with NOT CONNECTED. Stored drafts are Markdown/local records, external=false, not GitHub PRs. |
| Track real PR status | BLOCKED | Read helpers exist but are not wired into an active polling workflow. Webhook secret absent, no live deliveries, no external PRs. |
| Track associated bounty/payment | TEST ONLY / NOT CONNECTED for receipts | Simulated records are separate from live finances. Manual owner attestation is not provider receipt confirmation. No read-only cryptocurrency receipt integration. |
| Record all actions in Audit Log | WORKING for existing local stages; full coverage BLOCKED | 97 stored audit records and job events cover many local decisions. Reads/failures are not comprehensively logged; no remote lifecycle evidence. This read-only diagnostic wrote report artifacts, not new application audit events. |

## Additional blockers and truthfulness issue

- **Dashboard readiness label is misleading and remains unresolved.** `Dashboard.js` shows `System ready` whenever global_stop is false, regardless of GitHub, model, sandbox or payment state. Independently reproduced in owner and public views. This means the stop switch is off, not that real execution is ready. No application changes were made under diagnostic-only scope.
- Experiential Labs credential file exists with restrictive permissions, but its loader and live adapter are not wired into the app. Historical catalog authentication is not a fresh inference result. ModelRouter always rejects with 409. No model usage record/inference was created during diagnosis; the one-time allowance was not used.
- ASSISTED and all ordinary model/operating budgets remain $0. The $1 diagnostic allowance is not recurring funding for real jobs. The earlier approved gateway plan still requires an enforceable ceiling before its one inference; this diagnostic authorized no provider calls at all.
- Setting a GitHub token alone will NOT make live jobs work: the orchestrator blocks all non-simulated work, repository policies deny remote writes/private execution, and publish always refuses.
- No inference, clone, authenticated GitHub read, remote branch/commit/PR, live webhook, or real payment confirmation was claimed or performed.

## Minimum next actions in order — proposed, not executed

1. **User: choose or create one public disposable repository you own**, with a small issue and real tests. If none exists, GitHub `+` → `New repository`, then repository `Issues` → `New issue`. Do not begin with a third party's bounty repository.
2. **User + implementation work: connect GitHub safely.** GitHub avatar → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token. Select only the disposable repository, short expiration, and read-only metadata/issues initially (contents read for subsequent code inspection). The token must be injected into the backend through an approved untracked secret-loading path; current UI has no token entry or OAuth Connect button. Do not put the token in normal chat, tracked backend/.env, prompts, logs, or frontend. Secure loading requires implementation/configuration approval. Afterwards Penniless `Security` → `Verify connection` verifies identity; separate target-repo checks must establish permitted access. It does not authorize writes.
3. **Implementation work: complete the real worker.** Connect the production-grade isolated runner, authorized clone path, Experiential gateway with metering, general coding/testing/review workflow, and truthful readiness labels. Preserve zero-spend guards; any recurring paid job funding requires separate approval, not reuse of the $1 diagnostic allowance.
4. **User approval + implementation work: prove one controlled end-to-end job.** Only after preceding gates, separately approve required repository write permissions and one draft PR. Implement and test actual branch/commit/draft-PR delivery and read-only PR status synchronization (polling or separately configured signed webhook). Review evidence before publishing. No merge permission implied.
5. **Before real bounty revenue claims:** confirm actual reward/payment terms and a read-only receipt verification method. A public wallet address plus its network identifies a destination, not proof of receipt; it does not unlock spending. Coinbase stays excluded.

Future/backlog: external outcome/receipt reconciliation, safe revision loop, broader source support and learning optimization remain deferred. Suggested usability improvement: an evidence-linked readiness checklist showing each missing connection and the last successful verification, rather than a generic green ready label.

## Source references

- `backend/bounty/orchestrator.py`: non-simulated job block and local-only stages.
- `backend/bounty/workspace.py`, `sandbox_launcher.py`, `reviewers.py`: fixture-only implementation and execution boundaries.
- `backend/bounty/github.py`: transport methods and webhook skeleton, not active end-to-end delivery.
- `backend/bounty/routes.py`: GitHub identity check, publish block, repository write block, manual evidence flow.
- `backend/bounty/model_router.py`: always-blocking preflight.
- `backend/bounty/service.py`, `finance.py`, `store.py`: status, ledger separation and audit behavior.
- `frontend/src/bounty/Dashboard.js`, `Operations.js`, `Settings.js`: reproduced readiness wording and explicit local/integration warnings.
- GitHub setup reference: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens