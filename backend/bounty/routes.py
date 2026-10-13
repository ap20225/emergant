import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from pymongo.errors import DuplicateKeyError
from .models import (Document, OpportunityInput, OpportunityAction, ApprovalAction, ControlAction,
    Settings, GithubImport, PaymentInput, ExpenseInput, RepoPolicy, SourcePolicy, PaymentVerification)
from .store import Store, now
from .orchestrator import Orchestrator, ACTIVE
from .github import GitHub
from .service import Service
from .finance import financials, cents
from .evaluator import evaluate
from .model_router import ModelRouter
from . import fixture


class Result(BaseModel):
    model_config = ConfigDict(extra="allow")


def build_router(db, require_auth, get_current_user):
    store = Store(db)
    orchestrator = Orchestrator(store)
    github = GitHub(store)
    service = Service(store, orchestrator, github)
    router = APIRouter(prefix="/api/bounty")
    finance_lock = asyncio.Lock()

    async def owner(request: Request):
        return await require_auth(request)

    from .execution_routes import execution_router
    router.include_router(execution_router(store, orchestrator, github, owner))

    @router.get("/discovery/board", response_model=Result)
    async def discovery_board(user=Depends(owner)):
        from .classification import classify
        from .runnable import runnable_state, RUNNABLE
        opps = await store.rows("bounty_opportunities", {}, limit=500)
        items, lanes, prios, pays = [], {}, {}, {}
        runnable_now = 0
        tiers = {"REAL_EXECUTABLE_NOW": 0, "PAYMENT_UNVERIFIED": 0, "WATCHLIST_DISCOVERED": 0}
        for o in opps:
            c = classify(o)
            rs = runnable_state(o)
            if rs["state"] == RUNNABLE:
                runnable_now += 1
            if c["executability"] == "YES" and c["payability"] == "YES":
                tiers["REAL_EXECUTABLE_NOW"] += 1
            elif o.get("reward_amount") or o.get("authoritative_reward_amount") or (o.get("payout_certainty") not in (None, "UNKNOWN")):
                tiers["PAYMENT_UNVERIFIED"] += 1
            else:
                tiers["WATCHLIST_DISCOVERED"] += 1
            lanes[c["lane"]] = lanes.get(c["lane"], 0) + 1
            prios[c["priority"]] = prios.get(c["priority"], 0) + 1
            pays[c["payability"]] = pays.get(c["payability"], 0) + 1
            items.append({"id": o["id"], "source": o.get("source"), "repository": o.get("repository"),
                "issue_number": o.get("issue_number"), "title": o.get("title"),
                "reward_amount": o.get("reward_amount") or o.get("authoritative_reward_amount"),
                "reward_currency": o.get("reward_currency") or o.get("authoritative_reward_currency"),
                "reward_status": o.get("reward_status"), "payout_certainty": o.get("payout_certainty") or "UNKNOWN",
                "payout_authority": o.get("payout_authority"), "solver_count": o.get("solver_count"),
                "issue_created_at": o.get("issue_created_at"), "reward_created_at": o.get("reward_created_at"),
                "source_url": o.get("source_url"), "runnable": rs, **c})
        order = {"HOT": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        items.sort(key=lambda x: (order.get(x["priority"], 9), -x["priority_score"]))
        # Foreground the bounties that pass every stored gate right now.
        runnable_list = sorted((i for i in items if i["runnable"]["state"] == RUNNABLE),
                               key=lambda x: (order.get(x["priority"], 9), -x["priority_score"]))[:10]
        health = await store.rows("source_health", {}, limit=50)
        last_run = await store.rows("discovery_runs", {}, limit=1)
        last_scout_at = (last_run[0].get("finished_at") if last_run else None)
        return {"items": items, "source_health": health, "runnable_now_list": runnable_list,
                "last_scout_at": last_scout_at, "summary": {
            "total": len(items), "payable_yes": pays.get("YES", 0), "runnable_now": runnable_now,
            "executable_yes": sum(1 for i in items if i["executability"] == "YES"),
            "tiers": tiers, "lanes": lanes, "priorities": prios, "payability": pays}}

    @router.post("/discovery/run", response_model=Result)
    async def discovery_run(user=Depends(owner)):
        from .autonomy import run_discovery_cycle
        result = await run_discovery_cycle(store, actor=user.user_id)
        health = await store.rows("source_health", {}, limit=50)
        return {"result": result, "source_health": health}

    @router.get("/bootstrap", response_model=Result)
    async def bootstrap(request: Request):
        user = await get_current_user(request)
        if user:
            user = await require_auth(request)
        return await service.snapshot(user)

    @router.post("/safe-test/discover", response_model=Document)
    async def safe_discover(user=Depends(owner)):
        return await service.discover(fixture.opportunity(), user.user_id)

    @router.post("/opportunities", response_model=Document)
    async def add_opportunity(body: OpportunityInput, user=Depends(owner)):
        data = {**body.model_dump(), "source": "manual", "source_url": None, "simulated": False,
            "repository_size": 0, "labels": [], "requirements": [], "deadline": None, "payment_method": "UNKNOWN"}
        return await service.discover(data, user.user_id)

    @router.post("/github/import", response_model=Document)
    async def github_import(body: GithubImport, user=Depends(owner)):
        try:
            return await service.import_github(body.url, user.user_id)
        except HTTPException as exc:
            await store.audit("scout", "GitHub import blocked", str(exc.detail), "BLOCKED", actor=user.user_id)
            raise

    @router.post("/github/check", response_model=Result)
    async def github_check(user=Depends(owner)):
        result = await github.request("GET", "/user", private=True)
        await store.audit("security", "GitHub connection verified", "GitHub /user returned success; token values never stored in the audit", actor=user.user_id)
        return {"status": "CONNECTED", "login": result["login"]}

    @router.post("/webhooks/github", response_model=Result)
    async def webhook(request: Request):
        return await github.webhook(request)

    @router.post("/opportunities/{opportunity_id}/action", response_model=Document)
    async def action_opportunity(opportunity_id: str, body: OpportunityAction, user=Depends(owner)):
        o = await store.one("opportunities", {"id": opportunity_id})
        if not o:
            raise HTTPException(404, "Opportunity not found")
        if await store.one("jobs", {"opportunity_id": opportunity_id}):
            raise HTTPException(409, "An existing job owns this opportunity; use job controls")
        patch = {"status": body.action}
        if body.action == "EVALUATE":
            patch["evaluation"] = evaluate(o, await store.settings())
            patch["status"] = "EVALUATED" if patch["evaluation"]["qualified"] else "FLAGGED"
            await store.insert("opportunity_scores", {"opportunity_id": opportunity_id, **patch["evaluation"]})
        await db.opportunities.update_one({"id": opportunity_id}, {"$set": patch})
        await store.audit("evaluator", f"Opportunity {body.action.lower()}", o["title"], actor=user.user_id)
        return {**o, **patch}

    @router.post("/opportunities/{opportunity_id}/pursue", response_model=Document)
    async def pursue(opportunity_id: str, user=Depends(owner)):
        return await orchestrator.create_job(opportunity_id, user.user_id)

    @router.get("/jobs/{job_id}", response_model=Document)
    async def job_detail(job_id: str, user=Depends(owner)):
        return await service.job_detail(job_id)

    @router.post("/jobs/{job_id}/retry", response_model=Document)
    async def retry_job(job_id: str, user=Depends(owner)):
        return await orchestrator.retry(job_id, user.user_id)

    @router.post("/jobs/{job_id}/approval", response_model=Document)
    async def approval(job_id: str, body: ApprovalAction, user=Depends(owner)):
        async with orchestrator.lock:
            job = await store.one("jobs", {"id": job_id})
            if not job:
                raise HTTPException(404, "Job not found")
            if body.action == "ABORT":
                if job["status"] in ["PAID", "MERGED", "REJECTED", "CANCELLED"]:
                    raise HTTPException(409, "Completed jobs cannot be aborted")
                await orchestrator.stop(job_id)
                state, approval_state = "CANCELLED", "REJECTED"
            else:
                if job["status"] != "WAITING_APPROVAL":
                    raise HTTPException(409, "Job is not waiting for approval")
                if body.action == "APPROVE":
                    await orchestrator.gate(job_id)
                    c = await store.controls()
                    if c.get("submissions_disabled"):
                        raise HTTPException(409, "Submissions disabled")
                    if job["test_status"] != "PASS" or job["security_status"] != "PASS" or job["review_status"] != "APPROVE":
                        raise HTTPException(409, "All testing and review gates must pass")
                    state, approval_state = "APPROVED", "APPROVED"
                elif body.action == "REJECT":
                    state, approval_state = "REJECTED", "REJECTED"
                else:
                    if not body.reason.strip():
                        raise HTTPException(422, "Describe the requested changes")
                    state, approval_state = "CHANGES_REQUESTED", "CHANGES_REQUESTED"
            from .store import redact
            patch = {"status": state, "approval_status": approval_state, "approval_reason": redact(body.reason), "updated_at": now()}
            if body.action == 'APPROVE' and not job.get('simulated'):
                from .live_worker import artifact_hash
                if not job.get('patch_files') or artifact_hash(job['patch_files']) != job.get('artifact_hash'):
                    raise HTTPException(409, 'Reviewed patch missing or modified')
                patch['approved_artifact_hash'] = job['artifact_hash']
            await db.jobs.update_one({"id": job_id}, {"$set": patch})
            await db.approval_requests.update_many({"job_id": job_id}, {"$set": {"status": approval_state, "actor": user.user_id, "reason": redact(body.reason)}})
            await store.audit("master", f"Human decision: {body.action}", body.reason or "Owner decision on LOCAL reference artifact; no GitHub submission", job_id=job_id, actor=user.user_id, authorization="HUMAN_APPROVED" if body.action == "APPROVE" else "HUMAN_DECISION")
            return {**job, **patch}

    @router.post("/jobs/{job_id}/reference-outcome", response_model=Document)
    async def reference_outcome(job_id: str, user=Depends(owner)):
        async with orchestrator.lock:
            job = await store.one("jobs", {"id": job_id})
            if not job or not job["simulated"] or job["status"] != "APPROVED":
                raise HTTPException(409, "Only an approved local reference job can record a simulated merge")
            patch = {"status": "MERGED", "payment_status": "PENDING", "updated_at": now()}
            await db.jobs.update_one({"id": job_id}, {"$set": patch})
            await db.pull_requests.update_one({"job_id": job_id}, {"$set": {"status": "SIMULATED_MERGE"}})
            await store.insert("learning_records", {"job_id": job_id, "source": "safe-test", "language": "Python", "reward": job["reward"],
                "result": "SIMULATED_MERGE", "simulated": True, "cost": 0, "actual_hours": job["elapsed_seconds"] / 3600,
                "model": "deterministic-reference-worker", "payment": "PENDING", "lesson": "Reference validation only. Excluded from live scoring and revenue."})
            await store.audit("outcome", "Simulated merge recorded", "LOCAL test outcome; payment still pending; live revenue remains unchanged", job_id=job_id, actor=user.user_id)
            return {**job, **patch}

    @router.post("/jobs/{job_id}/publish", response_model=Result)
    async def publish(job_id: str, user=Depends(owner)):
        job = await store.one("jobs", {"id": job_id})
        if not job:
            raise HTTPException(404, "Job not found")
        await store.audit("pr_manager", "External submission blocked", "Reference artifacts cannot be pushed to external repositories. Production sandbox and credentialed workflow not connected.", "BLOCKED", job_id, user.user_id)
        raise HTTPException(409, "External submission NOT CONNECTED. Local reference drafts never publish to GitHub.")

    @router.post("/controls", response_model=Result)
    async def controls(body: ControlAction, user=Depends(owner)):
        mapping = {"GLOBAL_STOP": "global_stop", "STOP_AGENTS": "agents_stopped", "STOP_SPENDING": "spending_stopped", "DISABLE_SUBMISSIONS": "submissions_disabled", "DISABLE_NEW_JOBS": "new_jobs_disabled", "REVOKE_INTEGRATIONS": "integrations_revoked"}
        c = await store.controls()
        if body.action == "RESUME":
            c.update({key: False for key in mapping.values() if key != "integrations_revoked"})
        else:
            c[mapping[body.action]] = True
        await db.system_settings.update_one({"id": "controls"}, {"$set": {"config": c}})
        await store.audit("master", body.action.replace("_", " "), "Owner emergency control. Revocation disables application access; provider-side token revocation must be done at GitHub." if body.action == "REVOKE_INTEGRATIONS" else "Owner explicitly changed operational policy", actor=user.user_id, authorization="HUMAN_APPROVED")
        if body.action in ["GLOBAL_STOP", "STOP_AGENTS"]:
            await orchestrator.stop()
        return c

    @router.put("/settings", response_model=Settings)
    async def save_settings(body: Settings, user=Depends(owner)):
        async with finance_lock:
            finance = await financials(store)
            max_authorized = round(finance["verified_revenue"] * (100 - body.reserve_percent) / 100, 2)
            if body.authorized_budget > max_authorized:
                raise HTTPException(409, "Authorized budget cannot exceed verified non-reserve funds. Simulated funds are not spendable.")
            await db.system_settings.update_one({"id": "settings"}, {"$set": {"config": body.model_dump()}})
            for o in await store.rows("opportunities"):
                score = evaluate(o, body.model_dump())
                await db.opportunities.update_one({"id": o["id"]}, {"$set": {"evaluation": score}})
            await store.audit("master", "Settings updated", "Owner updated scoring, limits and allocation policy. Autonomous mode remains disabled in Phase 1.", actor=user.user_id)
            if body.mode == "OBSERVE":
                await orchestrator.stop()
            return body

    @router.get("/finance", response_model=Result)
    async def finance(simulated: bool = False, user=Depends(owner)):
        return await financials(store, simulated)

    @router.post("/payments", response_model=Document)
    async def payment(body: PaymentInput, user=Depends(owner)):
        async with finance_lock:
            job = await store.one("jobs", {"id": body.job_id})
            if not job or job["status"] not in ["MERGED", "PAYMENT_PENDING", "PAID", "UNPAID"]:
                raise HTTPException(409, "Payment recording requires a completed job")
            if body.verified and not body.attestation:
                raise HTTPException(422, "Explicit payment evidence attestation is required")
            status = "VERIFIED" if body.verified else "PENDING"
            doc = {"job_id": job["id"], "reward_expected": job["reward"], "reward_received": body.amount if body.verified else 0,
                "amount_cents": cents(body.amount), "currency": "USD", "reference": body.reference,
                "payment_reference": body.reference, "payment_method": body.method, "payment_status": status,
                "payment_date": now() if body.verified else None, "simulated": job["simulated"], "verified_by": user.user_id if body.verified else None,
                "verification_source": "SIMULATED" if job["simulated"] else "OWNER_ATTESTED_EVIDENCE"}
            try:
                result = await store.insert("payments", doc)
            except DuplicateKeyError:
                raise HTTPException(409, "Payment reference already recorded; duplicate recognition blocked")
            await store.insert("financial_transactions", {"payment_id": result["id"], "type": "REVENUE" if body.verified else "PENDING", "amount_cents": doc["amount_cents"], "simulated": job["simulated"]})
            if body.verified:
                await db.jobs.update_one({"id": job["id"]}, {"$set": {"status": "PAID", "payment_status": status}})
                await db.learning_records.update_many({"job_id": job["id"]}, {"$set": {"payment": status}})
            await store.audit("payment", "Payment evidence recorded", f"{status}; {'SIMULATED: excluded from live revenue' if job['simulated'] else 'owner-attested evidence, not provider-confirmed'}", job_id=job["id"], actor=user.user_id)
            return result

    @router.post("/expenses", response_model=Document)
    async def expense(body: ExpenseInput, user=Depends(owner)):
        async with finance_lock:
            if not body.simulated:
                c, f = await store.controls(), await financials(store)
                if c.get("global_stop") or c.get("spending_stopped") or cents(body.amount) > cents(f["available_budget"]):
                    await store.audit("finance", "Expense blocked", "Authorized live budget unavailable or spending stopped", "BLOCKED", actor=user.user_id)
                    raise HTTPException(409, "Expense exceeds authorized live operating budget or spending is stopped")
            try:
                doc = await store.insert("expenses", {**body.model_dump(), "amount_cents": cents(body.amount), "actor": user.user_id})
            except DuplicateKeyError:
                raise HTTPException(409, "Expense reference already recorded")
            await store.insert("financial_transactions", {"expense_id": doc["id"], "type": "EXPENSE", "amount_cents": doc["amount_cents"], "simulated": body.simulated})
            await store.audit("finance", "Expense recorded", f"{body.category}; {'SIMULATED' if body.simulated else 'LIVE'}", actor=user.user_id, cost=body.amount)
            return doc

    @router.post("/payments/{payment_id}/verify", response_model=Document)
    async def verify_payment(payment_id: str, body: PaymentVerification, user=Depends(owner)):
        async with finance_lock:
            payment = await store.one("payments", {"id": payment_id})
            if not payment:
                raise HTTPException(404, "Payment not found")
            if not body.attestation:
                raise HTTPException(422, "Explicit verification of receipt evidence is required")
            if payment["payment_status"] != "PENDING":
                raise HTTPException(409, "Only pending payments can be verified; duplicate recognition blocked")
            patch = {"payment_status": "VERIFIED", "reward_received": payment["amount_cents"] / 100,
                "payment_date": now(), "verified_by": user.user_id}
            await db.payments.update_one({"id": payment_id, "payment_status": "PENDING"}, {"$set": patch})
            await store.insert("financial_transactions", {"payment_id": payment_id, "type": "REVENUE_RECOGNITION", "amount_cents": payment["amount_cents"], "simulated": payment["simulated"]})
            await db.jobs.update_one({"id": payment["job_id"]}, {"$set": {"status": "PAID", "payment_status": "VERIFIED"}})
            await db.learning_records.update_many({"job_id": payment["job_id"]}, {"$set": {"payment": "VERIFIED"}})
            await store.audit("payment", "Pending evidence verified", "SIMULATED: excluded from live finances" if payment["simulated"] else "Owner-attested receipt evidence; not provider confirmation", job_id=payment["job_id"], actor=user.user_id, authorization="HUMAN_APPROVED")
            return {**payment, **patch}

    @router.put("/repositories/{repo_id}/policy", response_model=Document)
    async def repo_policy(repo_id: str, body: RepoPolicy, user=Depends(owner)):
        repo = await store.one("repositories", {"id": repo_id})
        if not repo:
            raise HTTPException(404, "Repository not found")
        if repo.get("private") or body.push_authorized:
            raise HTTPException(409, "Private repository execution and remote writes remain disabled in Phase 1")
        await db.repositories.update_one({"id": repo_id}, {"$set": body.model_dump()})
        await store.audit("security", "Repository policy updated", f"{repo['full_name']}: allowed={body.allowed}; remote writes disabled", actor=user.user_id)
        return {**repo, **body.model_dump()}

    @router.put("/sources/{source_id}/policy", response_model=Document)
    async def source_policy(source_id: str, body: SourcePolicy, user=Depends(owner)):
        source = await store.one("opportunity_sources", {"id": source_id})
        if not source:
            raise HTTPException(404, "Source not found")
        await db.opportunity_sources.update_one({"id": source_id}, {"$set": {"allowed": body.allowed}})
        await store.audit("security", "Source policy updated", f"{source_id}: allowed={body.allowed}", actor=user.user_id)
        return {**source, "allowed": body.allowed}

    @router.post("/model/preflight/{job_id}", response_model=Result)
    async def model_preflight(job_id: str, user=Depends(owner)):
        if not await store.one("jobs", {"id": job_id}):
            raise HTTPException(404, "Job not found")
        return await ModelRouter(store).preflight("coding", 0.05, job_id)

    return router, store, orchestrator