import re
import uuid
from datetime import datetime, timezone
from .models import Settings


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def contains_secret(text):
    """High-confidence secret detection for validating MODEL-GENERATED files.
    Flags real credential material (provider keys, AWS keys, PATs, bearer tokens, private keys)
    but NOT benign documentation that merely mentions the words token/password/secret."""
    t = str(text)
    if re.search(r"(?i)(?:sk-[\w-]{8,}|xpl_[\w-]+|e2b_[\w-]+|gh[pousr]_[\w]{10,}|github_pat_[\w]+|AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9._-]{12,})", t):
        return True
    if re.search(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----", t):
        return True
    # word-assignment only when the value looks like a real secret (long, high-entropy), not a placeholder
    return bool(re.search(r"(?i)(?:api[_-]?key|password|secret|token)\s*[=:]\s*['\"]?[A-Za-z0-9+/_-]{16,}['\"]?", t)
                and not re.search(r"(?i)(?:your[_-]|<|\{\{|example|placeholder|xxx|changeme|\$\{?[A-Z_]+\}?)", t))


def redact(text):
    text = re.sub(r"(?i)(?:sk-[\w-]{8,}|xpl_[\w-]+|e2b_[\w-]+|gh[pousr]_[\w]{10,}|github_pat_[\w]+|AKIA[0-9A-Z]{16}|Bearer\s+\S+)", "[REDACTED]", str(text))
    text = re.sub(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]", text)
    return re.sub(r"(?i)((?:api[_-]?key|password|secret|token)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text)


AGENTS = [
    ("master", "Master orchestrator", "Controls state, policy gates, scheduling and recovery", "Orchestration"),
    ("scout", "Opportunity scout", "Ingests approved sources; never executes repository code", "Discovery"),
    ("evaluator", "Bounty evaluator", "Scores economics and flags risky opportunities", "Discovery"),
    ("researcher", "Repository researcher", "Read-only inspection and implementation strategy", "Execution"),
    ("coder", "Coding agent", "Applies minimal scoped changes in a job workspace", "Execution"),
    ("tester", "Testing agent", "Independently executes tests and reports evidence", "Quality"),
    ("security", "Security reviewer", "Scans changes for secrets and unsafe operations", "Quality"),
    ("reviewer", "Code reviewer", "Independently validates scope and correctness", "Quality"),
    ("pr_manager", "PR manager", "Prepares local drafts; gates all external submissions", "Delivery"),
    ("outcome", "Outcome tracker", "Tracks PR results independently from payments", "Delivery"),
    ("payment", "Payment tracker", "Records payment evidence; never infers payment from merges", "Finance"),
    ("finance", "Finance manager", "Enforces authorized budget and verified revenue only", "Finance"),
    ("learning", "Learning agent", "Stores operational outcomes; cannot change security rules", "Learning"),
]

COLLECTIONS = "agents agent_runs opportunities opportunity_sources opportunity_scores jobs job_events repositories branches test_runs security_reviews code_reviews pull_requests payments expenses financial_transactions budgets agent_performance learning_records system_settings approval_requests audit_logs webhook_events integration_connections model_usage execution_attestations publication_attempts routing_decisions bounty_opportunities discovery_runs briefings escalations source_health repo_authorizations autonomous_budget".split()


class Store:
    def __init__(self, db):
        self.db = db

    async def insert(self, collection, doc):
        value = {"id": uid(collection.rstrip("s")), "created_at": now(), **doc}
        await self.db[collection].insert_one(dict(value))
        return value

    async def rows(self, collection, query=None, limit=250):
        return await self.db[collection].find(query or {}, {"_id": 0}).sort("created_at", -1).to_list(limit)

    async def one(self, collection, query):
        return await self.db[collection].find_one(query, {"_id": 0})

    async def settings(self):
        value = await self.one("system_settings", {"id": "settings"})
        return value["config"] if value else Settings().model_dump()

    async def controls(self):
        value = await self.one("system_settings", {"id": "controls"})
        return value["config"] if value else {}

    async def audit(self, agent, action, reason, result="SUCCESS", job_id=None, actor="system", cost=0, authorization="POLICY_ALLOWED"):
        event = await self.insert("audit_logs", {"timestamp": now(), "agent": agent, "action": action,
            "reason": redact(reason), "result": result, "job_id": job_id, "actor": actor,
            "cost": cost, "authorization_state": authorization})
        if job_id:
            await self.insert("job_events", {**event, "id": uid("event")})
        return event

    async def init(self):
        for name in COLLECTIONS:
            await self.db[name].create_index("id", unique=True)
        from .routing_policy import RoutingPolicy
        await self.db.system_settings.update_one({'id': 'model-routing'}, {'$setOnInsert': {'config': RoutingPolicy().model_dump(), 'updated_at': now()}}, upsert=True)
        await self.db.opportunities.create_index("dedup_key", unique=True)
        await self.db.jobs.create_index("opportunity_id", unique=True)
        await self.db.bounty_opportunities.create_index("duplicate_key", unique=True)
        # Migrate any legacy/invalid saved discovery query (Sept 2025 GitHub search enforcement rejects 'state:').
        await self.db.system_settings.update_one(
            {'id': 'discovery-config', 'config.query': {'$regex': 'state:'}},
            {'$set': {'config.query': 'is:issue is:open label:bounty'}})
        await self.db.payments.create_index([("reference", 1), ("simulated", 1)], unique=True)
        await self.db.expenses.create_index([("reference", 1), ("simulated", 1)], unique=True)
        await self.db.webhook_events.create_index("delivery_id", unique=True)
        await self.db.audit_logs.create_index("created_at")
        await self.db.system_settings.update_one({"id": "settings"}, {"$setOnInsert": {"id": "settings", "config": Settings().model_dump()}}, upsert=True)
        await self.db.system_settings.update_one({"id": "controls"}, {"$setOnInsert": {"id": "controls", "config": {
            "global_stop": False, "agents_stopped": False, "spending_stopped": False,
            "submissions_disabled": False, "new_jobs_disabled": False, "integrations_revoked": False}}}, upsert=True)
        for id_, name, role, group in AGENTS:
            await self.db.agents.update_one({"id": id_}, {"$setOnInsert": {"id": id_, "name": name, "role": role, "group": group}}, upsert=True)
        for id_, name, allowed in [("safe-test", "Local acceptance fixture", True), ("github", "GitHub Issues", False), ("manual", "Manually entered", False)]:
            await self.db.opportunity_sources.update_one({"id": id_}, {"$setOnInsert": {"id": id_, "name": name, "allowed": allowed, "type": "fixture" if id_ == "safe-test" else "api"}}, upsert=True)
        await self.db.repositories.update_one({"id": "safe-reference"}, {"$setOnInsert": {
            "id": "safe-reference", "full_name": "penniless-lab/slugify", "language": "Python", "private": False,
            "allowed": True, "push_authorized": False, "simulated": True, "size": 3,
            "description": "Controlled local acceptance repository. No remote or real bounty."}}, upsert=True)
        await self.db.budgets.update_one({"id": "operating"}, {"$setOnInsert": {"id": "operating", "starting_capital_cents": 0}}, upsert=True)