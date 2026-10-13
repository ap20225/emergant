import os
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError
from .models import OpportunityInput
from .store import now, AGENTS
from .evaluator import evaluate
from .finance import financials
from .workspace import Workspace
from .orchestrator import ACTIVE


class Service:
    def __init__(self, store, orchestrator, github):
        self.store, self.orchestrator, self.github = store, orchestrator, github

    async def discover(self, data, actor):
        c = await self.store.controls()
        if c.get("global_stop") or c.get("agents_stopped"):
            raise HTTPException(409, "Discovery stopped by emergency policy")
        key = f"{data['repository'].lower()}#{data['issue_number']}"
        existing = await self.store.one("opportunities", {"dedup_key": key})
        if existing:
            await self.store.audit("scout", "Duplicate discovery skipped", key, actor=actor)
            return existing
        for field in ["title", "description", "payment_terms"]:
            from .store import redact
            data[field] = redact(data.get(field, ""))
        score = evaluate(data, await self.store.settings())
        value = {**data, "dedup_key": key, "status": "EVALUATED" if score["qualified"] else "FLAGGED",
            "discovered_at": now(), "evaluation": score, "payment_verified": False}
        try:
            o = await self.store.insert("opportunities", value)
        except DuplicateKeyError:
            return await self.store.one("opportunities", {"dedup_key": key})
        await self.store.insert("opportunity_scores", {"opportunity_id": o["id"], **score})
        await self.store.audit("scout", "Opportunity discovered", f"{o['repository']} #{o['issue_number']}; source={o['source']}", actor=actor)
        await self.store.audit("evaluator", "Opportunity evaluated", f"Score {score['score']}; expected profit ${score['expected_profit']}; " + ("qualified" if score["qualified"] else "; ".join(score["reasons"])), "SUCCESS" if score["qualified"] else "BLOCKED", actor=actor)
        return o

    async def import_github(self, url, actor):
        parts = url.rstrip("/").split("/")
        repo, number = "/".join(parts[3:5]), int(parts[-1])
        meta, issue = await self.github.issue(repo, number)
        data = OpportunityInput(title=issue["title"], description=(issue.get("body") or "No issue description supplied")[:10000], repository=meta["full_name"],
            issue_number=number, reward=0, language=meta.get("language") or "Unknown", risk_score=50).model_dump()
        data.update({"source": "github", "source_url": issue["html_url"], "simulated": False,
            "labels": [l["name"] for l in issue.get("labels", [])], "deadline": None,
            "repository_size": meta.get("size", 0), "payment_method": "UNKNOWN", "requirements": [],
            "payment_terms": "", "github_state": issue["state"]})
        await self.store.db.repositories.update_one({"full_name": meta["full_name"]}, {"$setOnInsert": {
            "id": f"github-{meta['id']}", "full_name": meta["full_name"], "private": meta["private"], "allowed": False,
            "push_authorized": False, "language": meta.get("language"), "simulated": False, "size": meta["size"],
            "description": (meta.get("description") or "")[:500], "url": meta["html_url"], "default_branch": meta["default_branch"]}}, upsert=True)
        return await self.discover(data, actor)

    async def snapshot(self, user=None):
        s, c = await self.store.settings(), await self.store.controls()
        if user is None:
            from .models import Settings
            s, c = Settings().model_dump(), {}
        names = ["opportunities", "jobs", "pull_requests", "approval_requests", "repositories", "opportunity_sources", "audit_logs", "learning_records"]
        data = {name: await self.store.rows(name, limit=200) if user else [] for name in names}
        finance = await financials(self.store) if user else {k: 0 for k in ["gross_revenue", "verified_revenue", "expenses", "net_profit", "reserve", "reinvestment", "experimentation", "available_budget", "authorized_budget", "verified_payments", "level"]}
        live_o = [o for o in data["opportunities"] if not o.get("simulated")]
        live_j = [j for j in data["jobs"] if not j.get("simulated")]
        prs = [p for p in data["pull_requests"] if p.get("external")]
        merged = len([p for p in prs if p["status"] == "MERGED"])
        metrics = {"active_jobs": len([j for j in data["jobs"] if j["status"] in ACTIVE]), "discovered": len(live_o),
            "selected": len(live_j), "submitted": len(prs), "merged": merged,
            "acceptance_rate": round(100 * merged / len(prs), 1) if prs else None,
            "payment_rate": round(100 * finance["verified_payments"] / len(prs), 1) if prs else None,
            "average_reward": round(sum(o["reward"] for o in live_o) / len(live_o), 2) if live_o else 0,
            "average_cost": (None if any(j.get('cost') is None for j in live_j) else round(sum(j["cost"] for j in live_j) / len(live_j), 2)) if live_j else 0,
            "average_hours": round(sum(j["elapsed_seconds"] for j in live_j) / 3600 / len(live_j), 2) if live_j else 0,
            "pipeline_value": round(sum(j["expected_profit"] for j in live_j if j["status"] not in ["FAILED", "CANCELLED", "REJECTED", "PAID", "UNPAID"]), 2),
            "awaiting_approval": len([r for r in data["approval_requests"] if r["status"] == "WAITING_FOR_APPROVAL"])}
        agents = []
        for id_, name, role, group in AGENTS:
            work = next((j for j in data["jobs"] if j.get("current_agent") == id_ and j["status"] in ACTIVE), None)
            status = "BLOCKED" if c.get("global_stop") or c.get("agents_stopped") else ("WORKING" if work else ("ONLINE" if id_ == "master" else "IDLE"))
            agents.append({"id": id_, "name": name, "role": role, "group": group, "status": status, "job_id": work["id"] if work else None})
        integrations = {
            "github": "REVOKED" if c.get("integrations_revoked") else ("CONFIGURED" if os.environ.get("GITHUB_TOKEN") else "NOT CONNECTED"),
            "model": "BLOCKED · $0 BUDGET" if s["max_model_cost_per_job"] == 0 else "NOT CONNECTED",
            "webhook": "REVOKED" if c.get("integrations_revoked") else ("CONFIGURED" if os.environ.get("GITHUB_WEBHOOK_SECRET") else "NOT CONNECTED"),
            "sandbox": "REFERENCE ONLY" if Workspace.supported() else "NOT CONNECTED",
            "production_sandbox": "NOT CONNECTED", "payments": "MANUAL EVIDENCE ONLY", "legacy": "DEACTIVATED"}
        from .readiness import execution_status
        readiness = await execution_status(self.store) if user else {'status': 'NOT_READY', 'capabilities': {}, 'blockers': []}
        if user:
            integrations['github'] = readiness['capabilities']['github'].replace('_', ' ')
            integrations['model'] = readiness['capabilities']['model_gateway'].replace('_', ' ')
            integrations['production_sandbox'] = readiness['capabilities']['workspace'].replace('_', ' ')
        return {**data, "settings": s, "controls": c, "finance": finance, "metrics": metrics,
            "readiness": {'status': readiness['status'], 'capabilities': readiness['capabilities'], 'blockers': readiness['blockers']},
            "agents": agents, "integrations": integrations, "user": {"name": user.name, "email": user.email} if user else None,
            "preview": user is None, "server_time": now()}

    async def job_detail(self, job_id):
        job = await self.store.one("jobs", {"id": job_id})
        if not job:
            raise HTTPException(404, "Job not found")
        for key in ["test_runs", "security_reviews", "code_reviews", "job_events", "pull_requests", "payments"]:
            job[key] = await self.store.rows(key, {"job_id": job_id})
        job["previous_test_runs"] = [t for t in job["test_runs"] if t.get("attempt", 0) != job.get("iteration", 0)]
        job["test_runs"] = [t for t in job["test_runs"] if t.get("attempt", 0) == job.get("iteration", 0)]
        return job