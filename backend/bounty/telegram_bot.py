"""Telegram Command Bot — full conversational control over the Bounty Hunter platform.

Owner-only, AI-brained, proactive notifications. Uses Claude Sonnet 5.5 via the
Emergent Universal Key for natural-language interpretation of commands.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


# ---------------------------------------------------------------------------
# Telegram API helpers (raw httpx — no extra dependency needed)
# ---------------------------------------------------------------------------

async def tg_request(token: str, method: str, payload: dict | None = None):
    url = TELEGRAM_API.format(token=token, method=method)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload or {})
        data = r.json()
        if not data.get("ok"):
            log.warning("Telegram API %s failed: %s", method, data.get("description"))
        return data


async def tg_send(token: str, chat_id: int, text: str, reply_markup: dict | None = None, parse_mode="Markdown"):
    """Send a message. Auto-splits if > 4096 chars."""
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode}
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        await tg_request(token, "sendMessage", payload)


async def tg_answer_callback(token: str, callback_query_id: str, text: str = ""):
    await tg_request(token, "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text[:200]})


# ---------------------------------------------------------------------------
# Compact system snapshot (what the AI brain sees)
# ---------------------------------------------------------------------------

async def compact_snapshot(store):
    """Build a small JSON-safe dict summarizing the full system state."""
    s = await store.settings()
    c = await store.controls()
    from .autonomy import get_flags
    flags = await get_flags(store)

    # Driver state
    driver = await store.one("system_settings", {"id": "autonomous-driver-state"})
    driver_cfg = driver.get("config", {}) if driver else {}

    # Active jobs
    from .execution_controller import LIVE_STATES
    active_jobs = await store.rows("jobs", {"status": {"$in": LIVE_STATES + ["QUEUED"]}}, limit=10)
    jobs_summary = [{"id": j["id"], "repo": j.get("repository"), "issue": j.get("issue_number"),
                     "status": j["status"], "agent": j.get("current_agent")} for j in active_jobs]

    # Budget
    budget_doc = await store.one("system_settings", {"id": "execution-controls"})
    budget = budget_doc.get("config", {}) if budget_doc else {}

    # Discovery stats
    total_opps = await store.db.bounty_opportunities.count_documents({})
    qualified = await store.db.bounty_opportunities.count_documents({"status": {"$in": ["AWAITING_AUTHORIZATION", "QUALIFIED"]}})

    # Recent jobs
    recent = await store.rows("jobs", limit=5)
    recent_summary = [{"id": j["id"], "repo": j.get("repository"), "status": j["status"],
                       "error": str(j.get("error", ""))[:80]} for j in recent]

    # Repos
    repos = await store.rows("repo_authorizations", limit=20)
    repo_list = [{"upstream": r.get("upstream"), "fork": r.get("fork_repo"), "active": r.get("active")} for r in repos]

    # Cooldowns
    cooldown_count = await store.db.driver_candidate_backoff.count_documents({})

    return {
        "mode": s.get("mode", "OBSERVE"),
        "controls": {
            "global_stop": c.get("global_stop", False),
            "agents_stopped": c.get("agents_stopped", False),
            "spending_stopped": c.get("spending_stopped", False),
            "submissions_disabled": c.get("submissions_disabled", False),
        },
        "flags": {
            "autonomous_execution": flags.get("AUTONOMOUS_EXECUTION_ENABLED", False),
            "background_discovery": flags.get("SCHEDULER_BACKGROUND_ENABLED", False),
            "auto_escalation": flags.get("AUTO_ESCALATION_ENABLED", False),
        },
        "driver": {
            "status": driver_cfg.get("status", "UNKNOWN"),
            "reason": driver_cfg.get("reason"),
            "eligible": driver_cfg.get("eligible_count", 0),
            "active_job": driver_cfg.get("active_job"),
            "last_error": str(driver_cfg.get("last_error", ""))[:100],
        },
        "budget": {
            "daily_usd": round(budget.get("daily_budget_nano_usd", 0) / 1e9, 2),
            "per_job_usd": round(budget.get("per_job_cap_nano_usd", 0) / 1e9, 2),
            "autonomous_execution": budget.get("autonomous_execution", False),
        },
        "active_jobs": jobs_summary,
        "recent_jobs": recent_summary,
        "discovery": {"total": total_opps, "qualified": qualified, "cooldowns": cooldown_count},
        "repos": repo_list[:10],
    }


# ---------------------------------------------------------------------------
# AI Brain — Claude Sonnet 5.5 via Emergent Universal Key
# ---------------------------------------------------------------------------

BRAIN_SYSTEM_PROMPT = """You are the Bounty Hunter operations assistant — a terse, factual Telegram bot that controls an autonomous coding bounty platform. The owner sends you messages; you interpret them and decide what action to take.

AVAILABLE ACTIONS (return exactly one in your JSON response):
- reply_only: Just respond with information, no mutation.
- toggle_autonomous: {enabled: true/false} — Turn autonomous execution on/off.
- toggle_escalation: {enabled: true/false} — Turn auto-escalation on/off.
- toggle_discovery: {enabled: true/false} — Turn background discovery on/off.
- set_mode: {mode: "OBSERVE"|"ASSISTED"|"AUTONOMOUS"} — Change operating mode.
- set_budget: {field: "daily"|"per_job", usd: number} — Adjust budget.
- run_bounty: {opportunity_id: "bopp_..."} — Trigger a bounty run.
- stop_job: {job_id: "job_..."} — Cancel/stop an active job.
- emergency_stop: {} — Stop all operations immediately.
- emergency_resume: {} — Resume all operations.
- set_routing: {routine: [...], coding: [...], review: [...]} — Change model routing.

RESPONSE FORMAT (always valid JSON):
{
  "action": "action_name",
  "params": {},
  "reply": "Markdown-formatted reply text for the owner",
  "confirm_required": false
}

RULES:
- Be terse. Use emoji for status: 🟢 running, 🔴 stopped, 🟡 pending, ⚡ executing.
- Tables for lists, bullet points for status.
- Set confirm_required=true ONLY for: emergency_stop, budget increases above $50, cancelling active jobs, or changing mode to OBSERVE.
- When showing IDs, use `code` formatting.
- Never reveal API keys or secrets.
- If the user asks something ambiguous, reply_only with a clarification question.
- For read-only queries (status, jobs, etc.), use reply_only and format the data from the snapshot.
"""


async def ai_brain(store, user_message: str, chat_id: int):
    """Call Claude Sonnet 5.5 to interpret the user's message and decide an action."""
    snapshot = await compact_snapshot(store)

    from emergentintegrations.llm.chat import LlmChat, UserMessage

    api_key = os.environ.get("EMERGENT_LLM_KEY") or os.environ.get("EMERGENT_API_KEY")
    if not api_key:
        return {"action": "reply_only", "params": {}, "reply": "LLM key not configured. Cannot process natural language.", "confirm_required": False}

    chat = LlmChat(
        api_key=api_key,
        session_id=f"telegram-{chat_id}",
        system_message=BRAIN_SYSTEM_PROMPT,
    ).with_model("anthropic", "claude-sonnet-5-5")

    prompt = f"""Current system state:
```json
{json.dumps(snapshot, indent=1)}
```

Owner message: {user_message}

Respond with a single JSON object (action, params, reply, confirm_required). No extra text."""

    msg = UserMessage(text=prompt)

    try:
        response = await chat.send_message(msg)
        text = response.content if hasattr(response, "content") else str(response)
        # Extract JSON from the response
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        for key in ("action", "params", "reply", "confirm_required"):
            if key not in result:
                result[key] = {"action": "reply_only", "params": {}, "reply": text, "confirm_required": False}.get(key)
        return result
    except json.JSONDecodeError:
        return {"action": "reply_only", "params": {}, "reply": text if text else "I couldn't parse that. Try again?", "confirm_required": False}
    except Exception as e:
        log.exception("AI brain error")
        return {"action": "reply_only", "params": {}, "reply": f"Brain error: {str(e)[:200]}", "confirm_required": False}


# ---------------------------------------------------------------------------
# Slash-command handlers (fast, no LLM needed)
# ---------------------------------------------------------------------------

async def cmd_status(store):
    snap = await compact_snapshot(store)
    d = snap["driver"]
    status_emoji = {"RUNNING": "🟢", "IDLE": "🟡"}.get(d["status"], "🔴")
    lines = [
        f"*Bounty Hunter Status*",
        f"Mode: `{snap['mode']}`",
        f"Driver: {status_emoji} {d['status']} ({d.get('reason', '')})",
        f"Eligible: {d['eligible']} | Cooldowns: {snap['discovery']['cooldowns']}",
        f"Active jobs: {len(snap['active_jobs'])}",
        f"Discovery: {snap['discovery']['total']} total, {snap['discovery']['qualified']} qualified",
        f"Budget: ${snap['budget']['daily_usd']}/day, ${snap['budget']['per_job_usd']}/job",
        f"Autonomous: {'🟢' if snap['flags']['autonomous_execution'] else '🔴'}",
        f"Discovery: {'🟢' if snap['flags']['background_discovery'] else '🔴'}",
        f"Escalation: {'🟢' if snap['flags']['auto_escalation'] else '🔴'}",
    ]
    if d.get("last_error"):
        lines.append(f"Last error: _{d['last_error']}_")
    return "\n".join(lines)


async def cmd_jobs(store):
    snap = await compact_snapshot(store)
    if not snap["active_jobs"]:
        return "*Active Jobs*\nNo active jobs running."
    lines = ["*Active Jobs*"]
    for j in snap["active_jobs"]:
        emoji = {"CODING": "⚡", "TRIAGE": "🔍", "TESTING": "🧪", "REVIEW": "📝"}.get(j["status"], "🔄")
        lines.append(f"{emoji} `{j['id']}` — {j['repo']}#{j['issue']} — {j['status']} ({j.get('agent', '-')})")
    return "\n".join(lines)


async def cmd_discovery(store):
    snap = await compact_snapshot(store)
    d = snap["discovery"]
    return f"*Discovery Board*\nTotal opportunities: {d['total']}\nQualified/eligible: {d['qualified']}\nIn cooldown: {d['cooldowns']}"


async def cmd_budget(store):
    snap = await compact_snapshot(store)
    b = snap["budget"]
    return f"*Budget*\nDaily cap: ${b['daily_usd']}\nPer-job cap: ${b['per_job_usd']}\nAutonomous: {'🟢' if b['autonomous_execution'] else '🔴'}"


async def cmd_repos(store):
    snap = await compact_snapshot(store)
    if not snap["repos"]:
        return "*Repositories*\nNo authorized repositories."
    lines = ["*Authorized Repositories*"]
    for r in snap["repos"]:
        lines.append(f"{'🟢' if r.get('active') else '⚪'} {r.get('upstream')} -> {r.get('fork')}")
    return "\n".join(lines)


async def cmd_controls(store):
    snap = await compact_snapshot(store)
    c = snap["controls"]
    f = snap["flags"]
    lines = [
        "*Execution Controls*",
        f"Global stop: {'🔴 ON' if c['global_stop'] else '🟢 OFF'}",
        f"Agents stopped: {'🔴 ON' if c['agents_stopped'] else '🟢 OFF'}",
        f"Spending stopped: {'🔴 ON' if c['spending_stopped'] else '🟢 OFF'}",
        f"Submissions disabled: {'🔴 ON' if c['submissions_disabled'] else '🟢 OFF'}",
        f"Autonomous execution: {'🟢 ON' if f['autonomous_execution'] else '🔴 OFF'}",
        f"Background discovery: {'🟢 ON' if f['background_discovery'] else '🔴 OFF'}",
        f"Auto escalation: {'🟢 ON' if f['auto_escalation'] else '🔴 OFF'}",
    ]
    return "\n".join(lines)


async def cmd_logs(store):
    audits = await store.rows("audit_logs", limit=10)
    if not audits:
        return "*Recent Logs*\nNo audit entries."
    lines = ["*Recent Logs*"]
    for a in audits:
        lines.append(f"`{a.get('agent', '?')}` {a.get('action', '?')} — {str(a.get('reason', ''))[:60]}")
    return "\n".join(lines)


async def cmd_help(*_):
    return """*Bounty Hunter Bot*

*Commands:*
/status — System overview
/jobs — Active jobs
/budget — Budget info
/discovery — Discovery stats
/controls — Execution controls
/repos — Authorized repos
/logs — Recent audit logs
/run <id> — Run a bounty
/stop — Emergency stop
/mode <mode> — Set mode (OBSERVE/ASSISTED/AUTONOMOUS)
/help — This message

Or just type naturally — the AI brain understands plain English."""


SLASH_COMMANDS = {
    "/status": cmd_status,
    "/jobs": cmd_jobs,
    "/discovery": cmd_discovery,
    "/budget": cmd_budget,
    "/repos": cmd_repos,
    "/controls": cmd_controls,
    "/logs": cmd_logs,
    "/help": cmd_help,
    "/start": None,  # Handled specially
}


# ---------------------------------------------------------------------------
# Action executor — translates AI brain decisions into internal calls
# ---------------------------------------------------------------------------

async def execute_action(store, action: str, params: dict):
    """Execute an action decided by the AI brain. Returns a result message or None."""
    from . import autonomy

    if action == "reply_only":
        return None  # No mutation

    if action == "toggle_autonomous":
        await autonomy.set_flag(store, "AUTONOMOUS_EXECUTION_ENABLED", params.get("enabled", False), "telegram-owner")
        return f"Autonomous execution {'enabled' if params.get('enabled') else 'disabled'}."

    if action == "toggle_escalation":
        await autonomy.set_flag(store, "AUTO_ESCALATION_ENABLED", params.get("enabled", False), "telegram-owner")
        return f"Auto-escalation {'enabled' if params.get('enabled') else 'disabled'}."

    if action == "toggle_discovery":
        await autonomy.set_flag(store, "SCHEDULER_BACKGROUND_ENABLED", params.get("enabled", False), "telegram-owner")
        return f"Background discovery {'enabled' if params.get('enabled') else 'disabled'}."

    if action == "set_mode":
        mode = params.get("mode", "OBSERVE")
        if mode not in ("OBSERVE", "ASSISTED", "AUTONOMOUS"):
            return f"Invalid mode: {mode}"
        await store.db.system_settings.update_one({"id": "settings"}, {"$set": {"config.mode": mode}})
        await store.audit("master", f"Mode changed to {mode}", "via Telegram bot", actor="telegram-owner")
        return f"Mode set to `{mode}`."

    if action == "set_budget":
        field = params.get("field")
        usd = float(params.get("usd", 0))
        nano = int(usd * 1e9)
        key = "daily_budget_nano_usd" if field == "daily" else "per_job_cap_nano_usd"
        await store.db.system_settings.update_one({"id": "execution-controls"}, {"$set": {f"config.{key}": nano}})
        await store.audit("master", f"Budget {field} set to ${usd}", "via Telegram bot", actor="telegram-owner")
        return f"Budget `{field}` set to ${usd:.2f}."

    if action == "run_bounty":
        opp_id = params.get("opportunity_id")
        if not opp_id:
            return "Missing opportunity_id."
        from .execution_controller import ExecutionController
        # Get controller from store (we'll pass it in from the bot)
        opp = await store.one("bounty_opportunities", {"id": opp_id})
        if not opp:
            return f"Opportunity `{opp_id}` not found."
        return f"Bounty run requested for `{opp_id}`. Use the dashboard or wait for the driver to pick it up."

    if action == "stop_job":
        job_id = params.get("job_id")
        if not job_id:
            return "Missing job_id."
        await store.db.jobs.update_one({"id": job_id}, {"$set": {"status": "CANCELLED", "error": "Cancelled via Telegram bot"}})
        await store.audit("master", f"Job {job_id} cancelled", "via Telegram bot", actor="telegram-owner")
        return f"Job `{job_id}` cancelled."

    if action == "emergency_stop":
        await store.db.system_settings.update_one({"id": "controls"}, {"$set": {
            "config.global_stop": True, "config.agents_stopped": True, "config.spending_stopped": True}})
        await store.audit("master", "EMERGENCY STOP", "All operations halted via Telegram bot", actor="telegram-owner")
        return "🔴 *EMERGENCY STOP ACTIVATED* — All operations halted."

    if action == "emergency_resume":
        await store.db.system_settings.update_one({"id": "controls"}, {"$set": {
            "config.global_stop": False, "config.agents_stopped": False, "config.spending_stopped": False}})
        await store.audit("master", "Emergency resume", "Operations resumed via Telegram bot", actor="telegram-owner")
        return "🟢 *Operations resumed.*"

    if action == "set_routing":
        from .routing_policy import save_policy, RoutingPolicy
        policy = RoutingPolicy(
            routine=params.get("routine", ["qwen3.8-flash", "gemini-3.5-flash"]),
            coding=params.get("coding", ["gpt-5.1-codex", "qwen3-coder-flash"]),
            review=params.get("review", ["qwen3.8-flash", "gemini-3.5-flash"]),
        )
        await save_policy(store, policy, "telegram-owner")
        return "Routing policy updated."

    return f"Unknown action: {action}"


# ---------------------------------------------------------------------------
# Confirmation flow via inline keyboard
# ---------------------------------------------------------------------------

def confirmation_keyboard(action: str, params: dict):
    callback_data = json.dumps({"a": action, "p": params})
    if len(callback_data) > 64:
        callback_data = json.dumps({"a": action})  # Trim params if too long
    return {
        "inline_keyboard": [[
            {"text": "Confirm", "callback_data": f"confirm:{callback_data[:60]}"},
            {"text": "Cancel", "callback_data": "cancel"},
        ]]
    }


# ---------------------------------------------------------------------------
# Main Bot class
# ---------------------------------------------------------------------------

class TelegramBot:
    def __init__(self, store):
        self.store = store
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.owner_chat_id = None
        self._last_state = {}
        self._pending_confirmations = {}
        self._running = False
        self._task = None

    @property
    def enabled(self):
        return bool(self.token)

    async def _load_owner(self):
        doc = await self.store.one("system_settings", {"id": "telegram-owner"})
        if doc:
            self.owner_chat_id = doc.get("config", {}).get("chat_id")

    async def _save_owner(self, chat_id: int, user_info: dict):
        self.owner_chat_id = chat_id
        await self.store.db.system_settings.update_one(
            {"id": "telegram-owner"},
            {"$set": {"config": {"chat_id": chat_id, "user": user_info, "registered_at": datetime.now(timezone.utc).isoformat()}}},
            upsert=True,
        )

    async def register_webhook(self, base_url: str):
        if not self.enabled:
            return
        webhook_url = f"{base_url}/api/telegram/webhook"
        result = await tg_request(self.token, "setWebhook", {"url": webhook_url, "allowed_updates": ["message", "callback_query"]})
        log.info("Telegram webhook registered: %s -> %s", webhook_url, result.get("ok"))

    async def send(self, text: str, reply_markup: dict | None = None):
        if not self.owner_chat_id or not self.enabled:
            return
        await tg_send(self.token, self.owner_chat_id, text, reply_markup)

    async def handle_update(self, update: dict):
        """Process an incoming Telegram update."""
        if not self.enabled:
            return

        await self._load_owner()

        # Handle callback queries (confirmation buttons)
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return

        message = update.get("message")
        if not message or not message.get("text"):
            return

        chat_id = message["chat"]["id"]
        user = message.get("from", {})
        text = message["text"].strip()

        # /start — auto-register owner (first user to /start)
        if text.startswith("/start"):
            if self.owner_chat_id is None:
                await self._save_owner(chat_id, {"id": user.get("id"), "username": user.get("username"), "first_name": user.get("first_name")})
                await tg_send(self.token, chat_id, "Registered as instance owner. You now have full control.\n\nType /help to see commands, or just talk to me.")
                await self.store.audit("master", "Telegram owner registered", f"chat_id={chat_id}, user={user.get('username')}", actor="telegram-bot")
            elif chat_id == self.owner_chat_id:
                await tg_send(self.token, chat_id, "You're already registered. Type /help for commands.")
            else:
                return  # Silently ignore non-owners
            return

        # Owner verification — silently ignore non-owners
        if self.owner_chat_id and chat_id != self.owner_chat_id:
            return

        # If no owner registered yet, reject
        if not self.owner_chat_id:
            await tg_send(self.token, chat_id, "No owner registered. Send /start first.")
            return

        # Store message
        await self.store.db.telegram_messages.insert_one({
            "chat_id": chat_id, "direction": "incoming", "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Route: slash command or AI brain
        cmd = text.split()[0].lower().split("@")[0] if text.startswith("/") else None

        if cmd in SLASH_COMMANDS:
            if cmd == "/start":
                return
            handler = SLASH_COMMANDS[cmd]

            # Special slash commands with args
            if cmd == "/run" and len(text.split()) > 1:
                opp_id = text.split()[1]
                await tg_send(self.token, chat_id, f"Running bounty `{opp_id}`...")
                result = await execute_action(self.store, "run_bounty", {"opportunity_id": opp_id})
                await tg_send(self.token, chat_id, result or "Done.")
                return

            if cmd == "/stop":
                reply = "Are you sure you want to *EMERGENCY STOP* all operations?"
                markup = confirmation_keyboard("emergency_stop", {})
                await tg_send(self.token, chat_id, reply, reply_markup=markup)
                return

            if cmd == "/mode" and len(text.split()) > 1:
                mode = text.split()[1].upper()
                if mode == "OBSERVE":
                    reply = f"Change mode to `{mode}`? This will pause execution."
                    markup = confirmation_keyboard("set_mode", {"mode": mode})
                    await tg_send(self.token, chat_id, reply, reply_markup=markup)
                else:
                    result = await execute_action(self.store, "set_mode", {"mode": mode})
                    await tg_send(self.token, chat_id, result or "Done.")
                return

            if cmd == "/settings":
                reply = await cmd_status(self.store)
                await tg_send(self.token, chat_id, reply)
                return

            reply = await handler(self.store)
            await tg_send(self.token, chat_id, reply)
            return

        # AI Brain for natural language
        await tg_send(self.token, chat_id, "Thinking...")
        result = await ai_brain(self.store, text, chat_id)

        action = result.get("action", "reply_only")
        params = result.get("params", {})
        reply = result.get("reply", "")
        confirm = result.get("confirm_required", False)

        if confirm and action != "reply_only":
            self._pending_confirmations[chat_id] = {"action": action, "params": params}
            markup = confirmation_keyboard(action, params)
            await tg_send(self.token, chat_id, reply, reply_markup=markup)
        elif action != "reply_only":
            exec_result = await execute_action(self.store, action, params)
            final_reply = reply
            if exec_result:
                final_reply += f"\n\n{exec_result}"
            await tg_send(self.token, chat_id, final_reply)
        else:
            await tg_send(self.token, chat_id, reply)

        # Store outgoing
        await self.store.db.telegram_messages.insert_one({
            "chat_id": chat_id, "direction": "outgoing", "text": reply[:500],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def _handle_callback(self, callback_query: dict):
        cq_id = callback_query["id"]
        chat_id = callback_query["message"]["chat"]["id"]
        data = callback_query.get("data", "")

        if chat_id != self.owner_chat_id:
            await tg_answer_callback(self.token, cq_id, "Unauthorized")
            return

        if data == "cancel":
            await tg_answer_callback(self.token, cq_id, "Cancelled")
            await tg_send(self.token, chat_id, "Cancelled.")
            self._pending_confirmations.pop(chat_id, None)
            return

        if data.startswith("confirm:"):
            try:
                payload = json.loads(data[8:])
                action = payload.get("a")
                params = payload.get("p", {})
            except json.JSONDecodeError:
                # Fall back to pending confirmation
                pending = self._pending_confirmations.pop(chat_id, None)
                if pending:
                    action, params = pending["action"], pending["params"]
                else:
                    await tg_answer_callback(self.token, cq_id, "No pending action")
                    return

            await tg_answer_callback(self.token, cq_id, "Confirmed")
            result = await execute_action(self.store, action, params)
            await tg_send(self.token, chat_id, result or "Done.")
            return

        await tg_answer_callback(self.token, cq_id, "Unknown")

    # -----------------------------------------------------------------------
    # Proactive notifications — background polling loop
    # -----------------------------------------------------------------------

    def start(self):
        if not self.enabled or self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._notification_loop())
        log.info("Telegram notification loop started")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _notification_loop(self):
        """Poll every 15s and push diffs to the owner."""
        await asyncio.sleep(5)  # Let the app start up
        await self._load_owner()

        while self._running:
            try:
                await self._check_and_notify()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Notification loop error")
            await asyncio.sleep(15)

    async def _check_and_notify(self):
        if not self.owner_chat_id:
            await self._load_owner()
            if not self.owner_chat_id:
                return

        snap = await compact_snapshot(self.store)
        prev = self._last_state

        if not prev:
            self._last_state = snap
            return

        notifications = []

        # Job status changes
        prev_jobs = {j["id"]: j["status"] for j in prev.get("active_jobs", [])}
        curr_jobs = {j["id"]: j["status"] for j in snap.get("active_jobs", [])}

        # New jobs
        for jid, status in curr_jobs.items():
            if jid not in prev_jobs:
                job_info = next((j for j in snap["active_jobs"] if j["id"] == jid), {})
                notifications.append(f"🆕 *New job* `{jid}`\n{job_info.get('repo', '?')}#{job_info.get('issue', '?')} — {status}")

        # Status transitions
        for jid in set(prev_jobs) & set(curr_jobs):
            if prev_jobs[jid] != curr_jobs[jid]:
                job_info = next((j for j in snap["active_jobs"] if j["id"] == jid), {})
                emoji = {"CODING": "⚡", "TRIAGE": "🔍", "TESTING": "🧪", "REVIEW": "📝", "READY": "✅"}.get(curr_jobs[jid], "🔄")
                notifications.append(f"{emoji} *Job update* `{jid}`\n{prev_jobs[jid]} -> {curr_jobs[jid]}")

        # Jobs that disappeared from active (completed/failed)
        for jid in set(prev_jobs) - set(curr_jobs):
            recent = next((j for j in snap.get("recent_jobs", []) if j["id"] == jid), None)
            if recent:
                emoji = "✅" if recent["status"] in ("COMPLETED", "READY", "NEEDS_REVIEW") else "❌"
                err = f"\n_{recent['error']}_" if recent.get("error") else ""
                notifications.append(f"{emoji} *Job finished* `{jid}` — {recent['status']}{err}")

        # Driver state changes
        if prev.get("driver", {}).get("status") != snap.get("driver", {}).get("status"):
            d = snap["driver"]
            notifications.append(f"🤖 *Driver* {d['status']} ({d.get('reason', '')})")

        # Send notifications
        for note in notifications[:5]:  # Cap at 5 per cycle
            try:
                await self.send(f"🔔 {note}")
                await asyncio.sleep(0.5)
            except Exception:
                log.exception("Failed to send notification")

        self._last_state = snap
