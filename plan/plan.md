# OpenClaw Telegram Command Bot

A Telegram bot that gives the instance owner full conversational control over every aspect of the Bounty Hunter platform — discovery, execution, settings, repos, budget, driver, jobs — plus proactive push notifications for every lifecycle event. Connected to a top-tier LLM brain that interprets natural-language commands and translates them into internal API calls, so the owner never has to open the web dashboard.

Think of it as the dashboard in your pocket, with an AI co-pilot that understands "pause everything", "what's running?", "bump the budget to $50", or "run the top bounty" — and just does it.

## Who it's for

The sole instance owner (Telegram user linked to the bot token). No other users can interact. Every inbound message is verified against the owner's Telegram user ID before processing.

## Core features

- **Natural-language control.** Send a plain message like "show me all active jobs" or "turn off autonomous mode" and the AI brain translates it into the correct internal action. No slash-command memorisation needed.
- **Slash-command shortcuts.** Power-user commands for frequent actions: `/status`, `/jobs`, `/budget`, `/discovery`, `/run <id>`, `/stop`, `/mode <mode>`, `/settings`, `/repos`, `/controls`, `/logs`, `/help`.
- **Full operational surface.** Everything the web dashboard can do, the bot can do:
  - View and change execution controls (autonomous on/off, escalation, background discovery)
  - View and change mode (OBSERVE / ASSISTED / AUTONOMOUS)
  - View and adjust budget (daily cap, per-job cap, authorized budget)
  - View discovery board (counts, source health, runnable-now list)
  - View and manage jobs (active, history, cancel, approve, reject)
  - View and manage repos (list, pre-authorize fork, remove)
  - Trigger a bounty run manually (`/run <opportunity_id>`)
  - Emergency stop / resume all operations
  - View recommended bounty candidate
  - View review queue and act on items
  - View routing policy and change model preferences
  - View driver state, cooldowns, eligible count
- **Proactive notifications.** The bot pushes updates without being asked:
  - New bounty picked up by the autonomous driver
  - Job status changes (TRIAGE → CODING → TESTING → REVIEW → READY / NEEDS_REVIEW)
  - PR created or submitted
  - Job failure with error summary
  - Discovery cycle completed with new opportunity count
  - Budget threshold warnings (50%, 80%, 100% of daily cap)
- **AI brain decides, only asks when dangerous.** The LLM brain auto-executes safe actions (reads, toggles, status checks). It asks for confirmation only for: emergency stop, budget increases above $50, cancelling active jobs, changing mode to OBSERVE, or any action flagged as destructive.

## User flow

1. Owner sends the bot a message on Telegram (text or slash command).
2. Webhook delivers the message to the FastAPI backend (`/api/telegram/webhook`).
3. Owner ID is verified. Unauthorised senders are silently ignored.
4. The AI brain receives the message plus current system state (a compact snapshot), decides the action, and executes it via internal Python calls (not HTTP — direct function calls into the bounty service layer).
5. The bot replies with a formatted result (Markdown, concise, emoji-accented).
6. Independently, a background event watcher monitors job/driver state changes and pushes notifications to the owner's chat.

## UI / UX feel

- **Conversational, not menu-driven.** The bot talks like a sharp operations assistant — terse, factual, emoji for status at a glance (🟢 running, 🔴 stopped, 🟡 pending).
- **Responses are short.** Tables for lists, bullet points for status, code blocks for IDs. Never walls of text.
- **Confirmation prompts** are inline buttons (Telegram InlineKeyboard): [✅ Confirm] [❌ Cancel].
- **Proactive messages** are clearly labelled with a header like `🔔 Job Update` or `⚠️ Budget Warning` so the owner can glance and move on.

## Implementation phases

### Phase 1 — MVP (built now)

- Telegram webhook endpoint on the FastAPI backend (`/api/telegram/webhook`).
- Owner-only access gate (Telegram user ID check).
- AI brain powered by the Emergent Universal Key (Claude Sonnet 5.5 for text — fast, capable, already available). Receives the user message + a compact system snapshot, returns a structured action + reply.
- Full read surface: status, jobs, discovery board, execution controls, budget, repos, driver state, routing policy, recommended bounty, review queue.
- Full write surface: toggle autonomous/escalation/discovery, change mode, adjust budget, run bounty, cancel job, approve/reject review item, emergency stop/resume, change routing models.
- Slash-command shortcuts for the 12 most common operations.
- Proactive event notifications via a background polling loop (checks job/driver state every 15 s, pushes diffs to owner chat).
- Confirmation flow for destructive actions via InlineKeyboard callbacks.

### Phase 2 — Enrichment (future)

- Inline file/log sharing: send job execution logs, patch diffs, PR links as Telegram documents.
- Voice message support: owner sends a voice note → Whisper STT → AI brain processes → text reply.
- Scheduled digest: daily summary of completed/failed jobs, earnings, discovery stats.

### Phase 3 — Multi-channel (future)

- Discord mirror: same bot brain, Discord adapter.
- Web-socket live feed on the dashboard that mirrors the Telegram conversation.
- Multi-user roles: allow trusted collaborators limited bot access.

## Assumptions

- **LLM choice:** Claude Sonnet 5.5 via the Emergent Universal Key (already configured in the environment). Fast enough for chat, smart enough to parse ambiguous commands and map them to the correct internal action. No extra API key needed.
- **Webhook delivery:** The preview/production URL is HTTPS (required by Telegram). The webhook will be registered at startup and re-registered on deploy.
- **Polling for notifications (not DB change streams):** A background asyncio task polls job/driver state every 15 seconds and compares with the last-known state. Simpler than change streams and sufficient at this scale.
- **Owner Telegram user ID:** Will be auto-detected from the first `/start` message sent by the owner and persisted in the DB. No manual ID entry needed.
- **Bot token** stored in `backend/.env` as `TELEGRAM_BOT_TOKEN`.
- **No frontend changes.** The bot is entirely backend. The web dashboard continues to work independently.
- **Message history** stored in MongoDB (`telegram_messages` collection) for context continuity across conversations.
- **The AI brain calls internal Python functions directly** (the same service layer the web dashboard uses), not the HTTP API. This avoids auth cookie issues and is faster.
