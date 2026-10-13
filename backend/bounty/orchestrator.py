import asyncio
import json
import time
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError
from .evaluator import evaluate
from .store import now, uid, redact
from .workspace import Workspace
from . import reviewers

ACTIVE = ["SELECTED", "TRIAGE", "RESEARCHING", "CODING", "TESTING", "REVIEW", "READY"]


class Orchestrator:
    def __init__(self, store):
        self.store = store
        self.tasks = {}
        self.lock = asyncio.Lock()

    async def recover(self):
        for job in await self.store.rows("jobs", {"status": {"$in": ACTIVE}}):
            await self.store.db.jobs.update_one({"id": job["id"]}, {"$set": {"status": "NEEDS_REVIEW", "error": "Worker interrupted; workspace and evidence preserved. Owner review required."}})
            await self.store.audit("master", "Recovered interrupted job", "No unsafe automatic restart; workspace preserved", "BLOCKED", job["id"])

    async def gate(self, job_id=None, new=False):
        c, s = await self.store.controls(), await self.store.settings()
        reason = None
        if c.get("global_stop") or c.get("agents_stopped"):
            reason = "Agents stopped by emergency policy"
        elif s["mode"] == "OBSERVE":
            reason = "OBSERVE mode permits discovery/evaluation only"
        elif new and c.get("new_jobs_disabled"):
            reason = "New jobs are disabled"
        if reason:
            await self.store.audit("master", "Execution blocked", reason, "BLOCKED", job_id)
            raise HTTPException(409, reason)

    async def create_job(self, opportunity_id, actor):
        async with self.lock:
            await self.gate(new=True)
            o = await self.store.one("opportunities", {"id": opportunity_id})
            if not o:
                raise HTTPException(404, "Opportunity not found")
            previous = await self.store.one("jobs", {"opportunity_id": opportunity_id})
            if previous:
                raise HTTPException(409, f"Duplicate protected: existing attempt {previous['id']}")
            s = await self.store.settings()
            score = evaluate(o, s)
            source = await self.store.one("opportunity_sources", {"id": o["source"]})
            repo = await self.store.one("repositories", {"full_name": o["repository"]})
            reason = None
            if not score["qualified"]:
                reason = "; ".join(score["reasons"])
            elif not source or not source["allowed"] or not repo or not repo["allowed"]:
                reason = "Source and repository must both be explicitly allowlisted"
            elif not o.get("simulated"):
                reason = "Live execution BLOCKED: complete Security connections, select the actual test suite, and use controlled real execution. The reference worker never executes external repositories."
            elif await self.store.db.jobs.count_documents({"created_at": {"$gte": now()[:10]}}) >= s["max_jobs_per_day"]:
                reason = "Daily job limit reached; stop and wait"
            elif await self.store.db.jobs.count_documents({"status": {"$in": ACTIVE}}) >= s["max_concurrent_jobs"]:
                reason = "Concurrent worker limit reached; stop and wait"
            elif o.get("repository_size", 0) > s["max_repository_size"]:
                reason = "Repository size limit exceeded"
            if reason:
                await self.store.audit("master", "Job creation blocked", reason, "BLOCKED", actor=actor)
                raise HTTPException(409, reason)
            job = {"id": uid("job"), "opportunity_id": o["id"], "title": o["title"], "repository": o["repository"],
                "reward": o["reward"], "expected_profit": score["expected_profit"], "status": "SELECTED", "simulated": True,
                "created_at": now(), "updated_at": now(), "cost": 0, "elapsed_seconds": 0,
                "iteration": 0, "current_agent": "master", "files_changed": [], "test_status": "BLOCKED",
                "security_status": "BLOCKED", "review_status": "BLOCKED", "approval_status": "WAITING_FOR_APPROVAL"}
            try:
                await self.store.insert("jobs", job)
            except DuplicateKeyError:
                raise HTTPException(409, "Duplicate bounty attempt blocked")
            await self.store.db.opportunities.update_one({"id": o["id"]}, {"$set": {"status": "SELECTED"}})
            await self.store.audit("master", "Job selected", "Qualified controlled reference fixture; zero model cost; no remote access", job_id=job["id"], actor=actor)
            self.tasks[job["id"]] = asyncio.create_task(self.run(job, o))
            return job

    async def step(self, job, status, agent, reason):
        await self.gate(job["id"])
        await self.store.db.jobs.update_one({"id": job["id"]}, {"$set": {"status": status, "current_agent": agent, "updated_at": now()}})
        await self.store.audit(agent, status.replace("_", " ").title(), reason, job_id=job["id"])
        await self.store.insert("agent_runs", {"job_id": job["id"], "agent": agent, "stage": status})

    async def retry(self, job_id, actor):
        async with self.lock:
            await self.gate(job_id, new=True)
            job = await self.store.one("jobs", {"id": job_id})
            if not job or not job.get("simulated") or job["status"] != "NEEDS_REVIEW":
                raise HTTPException(409, "Only an interrupted/blocked reference job can be explicitly retried")
            s = await self.store.settings()
            opportunity = await self.store.one("opportunities", {"id": job["opportunity_id"]})
            repo = await self.store.one("repositories", {"full_name": job["repository"]})
            source = await self.store.one("opportunity_sources", {"id": opportunity["source"]})
            if not repo or not repo["allowed"] or not source or not source["allowed"] or not evaluate(opportunity, s)["qualified"]:
                raise HTTPException(409, "Retry no longer satisfies qualification/allowlist policy")
            if job.get("iteration", 0) >= s["max_fix_iterations"]:
                raise HTTPException(409, "Retry limit reached. Preserve evidence and request manual review.")
            if await self.store.db.jobs.count_documents({"status": {"$in": ACTIVE}}) >= s["max_concurrent_jobs"]:
                raise HTTPException(409, "Concurrent worker limit reached")
            patch = {"iteration": job.get("iteration", 0) + 1, "status": "SELECTED", "error": None,
                "test_status": "BLOCKED", "security_status": "BLOCKED", "review_status": "BLOCKED",
                "current_agent": "master", "elapsed_seconds": 0, "diff": None, "files_changed": [], "research": None}
            await self.store.db.jobs.update_one({"id": job_id}, {"$set": patch})
            await self.store.audit("master", "Human-authorized reference retry", f"Attempt {patch['iteration']}; previous workspace and logs archived, not discarded", job_id=job_id, actor=actor, authorization="HUMAN_APPROVED")
            updated = {**job, **patch}
            self.tasks[job_id] = asyncio.create_task(self.run(updated, opportunity))
            return updated

    async def record_tests(self, job, workspace, phase):
        result = await workspace.tests()
        status = "BLOCKED" if "Ran 7 tests" not in result["output"] or result["exit_code"] not in [0, 1] else ("PASS" if result["exit_code"] == 0 else "FAIL")
        record = await self.store.insert("test_runs", {"job_id": job["id"], "phase": phase, "status": status, "attempt": job.get("iteration", 0),
            "command": "python -B test_slugify.py (unprivileged chroot + seccomp)", **result,
            "checks": {"unit": status, "regression": status, "integration": "NOT_APPLICABLE", "build": "NOT_APPLICABLE", "lint": "NOT_RUN", "typecheck": "NOT_CONFIGURED"}})
        (workspace.root / "logs" / f"{phase}.log").write_text(result["output"])
        await self.store.audit("tester", f"{phase} tests", f"Actual exit code {result['exit_code']}; {status}", status, job["id"])
        return record

    async def run(self, job, opportunity):
        started = time.monotonic()
        workspace = Workspace(job["id"])
        try:
            s = await self.store.settings()
            async with asyncio.timeout(s["max_execution_time"]):
                await self.step(job, "RESEARCHING", "researcher", "Creating isolated reference workspace and read-only research")
                await workspace.create(job.get("iteration", 0))
                report = reviewers.research(workspace, opportunity)
                (workspace.root / "reports" / "research.json").write_text(json.dumps(report, indent=2))
                await self.store.db.jobs.update_one({"id": job["id"]}, {"$set": {"research": report, "workspace": str(workspace.root)}})
                baseline = await self.record_tests(job, workspace, "baseline")
                if baseline["status"] == "BLOCKED":
                    raise RuntimeError(f"Reference test runtime BLOCKED: {baseline['output'][:400]}")
                if baseline["status"] != "FAIL":
                    raise RuntimeError("Reference issue not reproduced; review required")
                await self.step(job, "CODING", "coder", "Creating a local working branch; applying a scoped reference fix")
                branch = f"bounty/{job['id']}"
                await workspace.git("checkout", "-b", branch)
                await self.store.insert("branches", {"job_id": job["id"], "name": branch, "repository": job["repository"], "remote": False})
                notes = reviewers.code(workspace)
                diff = await workspace.git("diff", "--", "slugify.py")
                files = (await workspace.git("diff", "--name-only")).splitlines()
                (workspace.root / "artifacts" / "changes.patch").write_text(diff)
                await self.store.db.jobs.update_one({"id": job["id"]}, {"$set": {"branch": branch, "implementation": notes, "diff": diff, "files_changed": files}})
                await self.step(job, "TESTING", "tester", "Independent regression suite running inside the restricted reference sandbox")
                tested = await self.record_tests(job, workspace, "regression")
                await self.store.db.jobs.update_one({"id": job["id"]}, {"$set": {"test_status": tested["status"]}})
                if tested["status"] != "PASS":
                    raise RuntimeError("Regression failed. No safe additional reference fix available; escalate without repeating an identical patch.")
                await self.step(job, "REVIEW", "security", "Independent static security review of actual source and diff")
                source = (workspace.repo / "slugify.py").read_text()
                security = await self.store.insert("security_reviews", {"job_id": job["id"], **reviewers.security_review(source, diff, files)})
                await self.store.db.jobs.update_one({"id": job["id"]}, {"$set": {"security_status": security["status"]}})
                await self.store.audit("security", "Security review completed", security["scope"], security["status"], job["id"])
                await self.step(job, "REVIEW", "reviewer", "Independent API, scope and regression-evidence review")
                review = await self.store.insert("code_reviews", {"job_id": job["id"], **reviewers.code_review(source, files, tested, diff)})
                await self.store.db.jobs.update_one({"id": job["id"]}, {"$set": {"review_status": review["status"]}})
                if security["status"] != "PASS" or review["status"] != "APPROVE":
                    raise RuntimeError("Review gate did not pass; submission blocked")
                await self.step(job, "READY", "pr_manager", "Preparing a local commit and PR artifact; not creating a GitHub PR")
                await workspace.git("add", "--", "slugify.py")
                await workspace.git("commit", "-m", "fix: normalize whitespace in slugify")
                commit = await workspace.git("rev-parse", "HEAD")
                body = "## Summary\nNormalize consecutive whitespace in slug generation.\n\n## Changes\n- Replace literal-space substitution with whitespace split/join.\n- Preserve the function signature and Unicode behavior.\n\n## Testing\n- Reproduced baseline regression failures.\n- Executed all 7 unittest regression cases: PASS.\n- Independent static reference security and scope reviews passed.\n- Lint/type checking/build: not configured or not applicable.\n\n## Execution context\nControlled local acceptance fixture. No GitHub PR has been created. No paid inference used."
                pr = await self.store.insert("pull_requests", {"job_id": job["id"], "repository": job["repository"], "title": "fix: normalize consecutive whitespace in slugify",
                    "body": body, "branch": branch, "commit": commit, "status": "LOCAL_DRAFT", "external": False, "url": None, "simulated": True})
                (workspace.root / "artifacts" / "pull-request.md").write_text(body)
                await self.store.insert("approval_requests", {"job_id": job["id"], "pr_id": pr["id"], "status": "WAITING_FOR_APPROVAL"})
                await self.step(job, "WAITING_APPROVAL", "master", "Local PR artifact ready for human review. No external submission or revenue.")
                await self.store.db.jobs.update_one({"id": job["id"]}, {"$set": {"commit": commit, "pr_id": pr["id"]}})
        except asyncio.CancelledError:
            await self.store.db.jobs.update_one({"id": job["id"]}, {"$set": {"status": "CANCELLED", "error": "Stopped by owner; evidence preserved"}})
            await self.store.audit("master", "Job cancelled", "Worker stopped; workspace preserved", "BLOCKED", job["id"])
        except Exception as exc:
            reason = redact(exc.detail if isinstance(exc, HTTPException) else str(exc))
            await self.store.db.jobs.update_one({"id": job["id"]}, {"$set": {"status": "NEEDS_REVIEW", "error": reason}})
            await self.store.audit("master", "Job stopped for review", reason, "BLOCKED", job["id"])
        finally:
            await self.store.db.jobs.update_one({"id": job["id"]}, {"$set": {"elapsed_seconds": round(time.monotonic() - started, 2), "current_agent": None, "updated_at": now()}})
            self.tasks.pop(job["id"], None)

    async def stop(self, job_id=None):
        tasks = [task for id_, task in list(self.tasks.items()) if not job_id or id_ == job_id]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)