# 🤖 Telegram AI Priority Filter & Digest Bot

> **An intelligent, context-aware Telegram notification gatekeeper and periodic digest assistant powered by Ollama.**

```text
Incoming Telegram Messages (DMs, Groups, Channels)
                    ↓
           Zero-Token Pre-Filter
      (Stickers, Emojis, "lol", "ok")
                    ↓
        Contextual AI Priority Classifier
        (Evaluates Sender, Chat & Rules)
                    ↓
      ┌─────────────┴─────────────┐
      ↓                           ↓
🚨 High Priority (P0 / P1)     🟡 / 🟢 Lower Priority (P2 / P3)
  Score >= 90                   Score < 90
      ↓                           ↓
Instant Telegram Push Alert     Periodic 3-Tier Digest
 (With Action Items & Links)   (Scheduled at 08:00, 13:00, 19:00, 22:00)
```

---

## ✨ Features

- 🎧 **Userbot Ingestion (`Telethon`):** Listens across all your personal 1-on-1 direct messages, university channels, and private group chats.
- 🧠 **Context-Aware AI:** Distinguishes between critical work (*"Meeting moved to 3 PM"* in CloudKH group $\rightarrow$ **P1**) and casual banter (*"Match moved to 3 PM"* in friends group $\rightarrow$ **P3**).
- ⚡ **Zero-Token Pre-Filter:** Automatically intercepts stickers, reactions, and one-word messages without consuming AI API credits.
- 🔥 **Situation Understanding:** Fuses related messages into one tracked issue with a status instead of scoring each message alone — five messages about the same broken AC become one situation, not five unrelated alerts. See [Situations](#situations).
- 📋 **Management Briefs & To-Do:** `/brief`, `/today`, `/yesterday`, `/week`, `/month` answer "what happened?" and "what do I need to do?" from tracked situations, not a fresh summarization pass each time.
- ⚠️ **Repeated-Issue Alerts:** A one-time warning when a chat reports the same kind of problem three or more times in a week.
- 📅 **Google Calendar + Gmail:** Real OAuth-connected Calendar event creation and Gmail summarize/send, approved by the owner before anything is created or sent. See [Google Calendar + Gmail](#google-calendar--gmail).
- 📎 **Document Intelligence:** PDF, Excel, Word (`.docx`), and OCR for images/scanned documents (menus, invoices, quotations) through the configured AI provider.
- 📊 **Weekly Meeting Deck:** `/weeklyreport` generates a 9-slide `.pptx` from the week's tracked situations and pending approvals.
- 🗂️ **Work Management:** Projects, tasks with a real workflow status machine, dependencies, and Critical Path Method scheduling — see [Business assistant structure](#business-assistant-structure).
- 🤖 **Conversational Agent:** A bounded, policy-checked tool-calling assistant for ordinary natural-language requests, not just slash commands — see [`docs/agent-architecture.md`](docs/agent-architecture.md).
- 📱 **Interactive Telegram Dashboard:** Chat directly with your bot in Telegram with one-tap buttons for digests, priority tiers, situations, briefs, projects, and more.
- 💬 **Ask AI About Your Chats:** Type any question to your bot (e.g. *"What did my lecturer announce today?"* or *"Any updates on CloudKH?"*) and get instant answers based on captured message context.
- 🔌 **Ollama AI:** Uses Ollama's OpenAI-compatible API for local models and Ollama Cloud models.

---

## 🚀 Quick Setup Guide (5 Minutes)

### 1. Prerequisites
- Python 3.10+ (or [uv](https://github.com/astral-sh/uv))

### 2. Install Dependencies
```powershell
# Create virtual environment
uv venv
.venv\Scripts\activate

# Install dependencies
uv pip install -e .
```

---

### 3. Get Your Credentials

You only need **3 things** to configure:

#### A. Telegram Client API (from [my.telegram.org](https://my.telegram.org))
1. Log in with your phone number on [my.telegram.org](https://my.telegram.org).
2. Click **API Development Tools**.
3. Create an application (App title: `AIFilter`, Short name: `aifilter`, Platform: `Desktop`).
4. Copy your **`App api_id`** and **`App api_hash`**.

#### B. Telegram Notification Bot (from [`@BotFather`](https://t.me/BotFather))
1. In Telegram, open [`@BotFather`](https://t.me/BotFather) and send `/newbot`.
2. Name your bot and copy the **HTTP API Token** (e.g., `8710137004:AAFX...`).
3. Open [`@userinfobot`](https://t.me/userinfobot) to get your personal **Chat ID** (e.g., `1265124779`).
4. **Important:** Open a direct message with your new bot and click **Start**!

#### C. Ollama (local or Cloud)
1. Choose a local Ollama model or create an API key at [Ollama Keys](https://ollama.com/settings/keys) for Ollama Cloud.
2. Use an Ollama model available to your account, such as `gpt-oss:120b` for direct Cloud API access.

---

### 4. Configure `.env`

Copy `.env.example` to `.env`:
```powershell
copy .env.example .env
```

Fill in your values in `.env`:

```env
# 1. Telegram Client (from my.telegram.org)
TELEGRAM_API_ID=38415799
TELEGRAM_API_HASH=your_api_hash_here
TELEGRAM_PHONE=+855XXXXXXXX

# 2. Notification Bot (from BotFather and userinfobot)
TELEGRAM_BOT_TOKEN=8710137004:AAFX...
NOTIFICATION_CHAT_ID=1265124779

# 3. AI Provider (Ollama)
AI_PROVIDER=ollama
OLLAMA_BASE_URL=https://ollama.com/v1
OLLAMA_MODEL=gpt-oss:120b
OLLAMA_API_KEY=your_ollama_api_key_here

# 4. Priority & Alert Rules
URGENT_SCORE_THRESHOLD=90
IMMEDIATE_ALERT_PRIORITIES=P0,P1
DIGEST_SCHEDULE_TIMES=08:00,13:00,19:00,22:00
```

---

## 🎮 How to Run

### 1. Start the Live Listener & Interactive Bot
```powershell
.venv\Scripts\python.exe main.py run
```
*On first startup, Telegram will send a login code to your Telegram app. Enter it in the terminal to save your login session.*

### 2. Docker + MongoDB deployment

MongoDB is the only runtime storage backend. Set `MONGO_URI` and start the stack:

```powershell
docker compose up -d --build
docker compose logs -f bot
```

The Compose file stores Telegram messages, classifications, business data, and assistant state in MongoDB. Stop the stack with `docker compose down`; do not add `-v` unless you intend to delete MongoDB data.

To migrate an existing SQLite database before the MongoDB-only cutover, stop the bot and run the idempotent migration once:

```powershell
docker compose stop bot
docker compose run --rm bot python migrate_sqlite_to_mongo.py --sqlite /app/data/telegram_bot.db --mongo-uri mongodb://mongo:27017 --mongo-database telegram_business
docker compose up -d bot
```

On the first Docker deployment, authorize the Telethon user session from an attached terminal. A detached container cannot answer Telegram's login-code prompt:

```powershell
docker compose down
docker compose run --rm bot
```

Enter the Telegram login code (and the two-step password if requested). Once the log shows that the userbot and interactive bot are connected, press `Ctrl+C`; the session files will be saved in `./runtime`. Start the background service afterward:

```powershell
docker compose up -d bot
docker compose logs -f bot
```

If you already authorized the bot outside Docker, copy `telegram_ai_session.session` and `bot_listener_session.session` into `./runtime` before starting Compose. Keep `./runtime` and the `mongo_data` volume; deleting them forces a new Telegram login or removes persisted business state.

### 2. Chat with Your Bot on Telegram
Open your bot in Telegram and send:
```text
/start
```
Use the interactive inline buttons or ask questions conversationally!

Use `/id` if you need to display your Telegram user ID while configuring `OWNER_USER_ID`.

---

## 🛠️ CLI Commands

| Command | Description |
| :--- | :--- |
| `python main.py run` | Start the live Userbot listener, interactive bot, and digest scheduler |
| `python main.py test` | Run the simulation test suite on sample messages |
| `python main.py digest` | Trigger an immediate manual digest of pending messages |
| `python main.py stats` | View database statistics (total messages, noise filtered, pending queue) |
| `python main.py dashboard` | Start the authenticated business management dashboard |
| `python main.py backfill --days 7 --limit 50` | Import older Telegram messages into the classifier and history database |
| `python migrate_sqlite_to_mongo.py --mongo-uri ...` | Copy existing SQLite records into MongoDB before switching storage |

---

## 🎯 Customizing Priority Rules (`user_profile.yaml`)

Edit `user_profile.yaml` to customize what matters to you:

```yaml
user_name: "Choonvai"

high_priority_rules:
  - "University lecturer, professor, or teaching assistant messages"
  - "Assignment, exam, course registration, or deadline changes"
  - "CloudKH project decisions, blockers, code reviews, and architecture proposals"
  - "Work, client, freelance, or commercial contract requests"
  - "Payment, banking, invoice, billing, or supplier critical issues"

medium_priority_rules:
  - "General project progress updates"
  - "Meeting notes, schedule summaries, or agendas"

low_priority_rules:
  - "Casual chatting, greetings, jokes, banter, sports match discussions"
  - "Memes, stickers, GIF reactions, emoji-only responses"

vip_senders:
  - "boss"
  - "lecturer"
  - "professor"
  - "client"

important_projects:
  - "CloudKH"
  - "University"
```

---

## 📂 Project Structure

```text
├── config.py              # Configuration manager & YAML loader
├── user_profile.yaml      # Personalized user priority rules
├── app/core/models.py     # Pydantic message and digest schemas
├── app/storage/sqlite_messages.py # SQLite migration/test compatibility engine
├── app/priority/prefilter.py # Zero-token rule pre-filter
├── app/priority/classifier.py # AI priority classifier
├── app/telegram/notifier.py # Telegram alert dispatcher
├── app/priority/digest.py # 3-Tier periodic digest aggregation engine
├── app/telegram/scheduler.py # APScheduler background digest timer
├── main.py                # Main CLI daemon & interactive Telegram Bot
├── tests/                 # Unit and compatibility tests
└── pyproject.toml         # Dependencies and metadata
```

## Business assistant structure

The maintainable business features live under [`app/`](app/):

```text
app/
├── agent/       # Bounded decision runtime, policy, context, and tools
├── core/        # Shared domain schemas
├── priority/    # Message filtering, classification, and digests
├── telegram/    # Telegram delivery and scheduling adapters
├── ai/          # Ollama-compatible provider adapter
├── assistants/  # Assistant profiles and routing
├── content/     # Bounded text, PDF, and image handling
├── dashboard/   # Authenticated dashboard data services
├── projects/    # Project planning and Critical Path Method calculation
├── security/    # Approved users, groups, and pairing policy
├── services/    # Use cases that coordinate the application
├── storage/     # MongoDB runtime repositories and SQLite migration support
└── usage/       # Shared AI budget and usage accounting
```

Telegram handlers translate events into `app.services` use cases. Providers never decide access, storage never sends messages, and every AI operation is recorded against the shared budget. See [`docs/architecture.md`](docs/architecture.md) and [`docs/client-video-feature-scope.md`](docs/client-video-feature-scope.md) for the delivery phases.

For the business assistant, set `AI_PROVIDER=ollama`, provide `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, and `OLLAMA_API_KEY`. Set `OWNER_USER_ID` to the administrator's Telegram user ID; optionally provide comma-separated `APPROVED_USER_IDS` and `APPROVED_GROUP_IDS`. `AI_MONTHLY_BUDGET_USD` defaults to `20.0`.

To use Ollama Cloud directly, create an API key at [Ollama Keys](https://ollama.com/settings/keys) and set `AI_PROVIDER=ollama`, `OLLAMA_BASE_URL=https://ollama.com/v1`, `OLLAMA_MODEL` to a model available in your Ollama account (for example `gpt-oss:120b`), and `OLLAMA_API_KEY` to the key. Keep the key in `.env`, which is ignored by Git. If Ollama is running locally on the Windows host while this bot runs in Docker, use `OLLAMA_BASE_URL=http://host.docker.internal:11434/v1` and sign in locally with `ollama signin`; the bot container should not use `localhost` for the host service.

`AI_REQUEST_TIMEOUT_SECONDS` defaults to `120` to give Cloud models enough time to respond. Direct Cloud API calls use models such as `gpt-oss:120b`; names ending in `:cloud` are used when a local Ollama installation offloads the model to Cloud.

Natural-language messages use the bounded work-management agent described in [docs/agent-architecture.md](docs/agent-architecture.md). It reads only owner-selected Telegram chats and can use the existing project/work-item service through typed, policy-checked tools. Creation and destructive or domain-specific inventory/sales actions remain outside this first slice.

Telegram business commands include `/setup`, `/setup search NAME`, `/setup user TELEGRAM_USER_ID`, `/department`, `/departments`, `/project`, `/projects`, `/ptask`, `/assign`, `/status`, `/dep`, `/milestone`, `/comment`, `/plan` (CPM), `/task`, `/tasks`, `/done`, `/remind`, `/reminders`, `/web URL [question]`, `/draft CHAT_ID message`, `/email recipient | subject | message`, `/event start | title | optional end`, `/connectgoogle`, `/googlestatus`, `/disconnectgoogle`, `/calendar`, `/inbox`, `/situations`, `/brief`, `/today`, `/yesterday`, `/week`, `/month`, `/weeklyreport`, `/assistant [id]`, `/memory [search]`, `/new`, `/forget all`, and `/integrations`. Outbound drafts always wait for an owner approval button before delivery. `/web` is read-only and limited to public HTTP(S) sources.

Use `/setup` in the owner's private chat to open the focus wizard. Setup is required before the first message is inspected: with no saved focus list, the userbot returns immediately and the assistant does not process or classify any chat. Choose **Select groups** for AIC and other internal groups, or **Select people** for Daivai, suppliers, and customers. In either picker, tap **Search**, type a name directly (for example `daivai`), and select the matching result. Set each chat to **Monitor**, **Mention only**, or **Orders/tasks**, add approved members, and press **Save setup**. If the picker cannot load dialogs, add the bot to the target group and send `/setup here` there as the owner; the group is added without processing its history. Once saved, the userbot sends only the selected chats through classification and alerts, and all digests, priority views, statistics, and AI context use the same selected-chat scope. The AIC group can be used as the internal workspace while supplier and customer chats remain separate focused sources. The owner can add a member with `/setup user TELEGRAM_USER_ID`; access is still controlled by the normal approval policy.

CPM uses each task's duration in days and finish-to-start dependencies. `/plan PROJECT_ID` reports project duration, completion percentage, early/late dates, float, and the critical task path; dependency cycles are rejected.

`ASSISTANT_IDS` creates separate assistant contexts in storage (for example `business,sales,support`). Optional email, calendar, CRM, and task connectors use signed webhook endpoints (`*_WEBHOOK_URL` plus `INTEGRATION_SECRET`) and remain disabled until configured. The management dashboard is delivered as a Telegram Web App: set `DASHBOARD_PUBLIC_URL` to the public HTTPS URL and `DASHBOARD_TOKEN`; the bot then shows an **Open Project Workspace** button inside Telegram. Its authenticated APIs expose status, users, groups, departments, projects, CPM plans, assistants, schedules, permissions, pending approvals, and task views through `/api/status`, `/api/users`, `/api/groups`, `/api/departments`, `/api/projects`, `/api/projects/{id}/plan`, `/api/assistants`, `/api/schedules`, `/api/permissions`, `/api/actions`, and `/api/tasks`.

Set `DASHBOARD_TOKEN` and `DASHBOARD_PUBLIC_URL` before starting the bot. The Web App must be served through HTTPS; in Docker the dashboard listens on port `3141`, so point your existing reverse proxy/domain to that port. The standalone `python main.py dashboard` command remains available for administration and diagnostics.

### Google Calendar + Gmail

Owner-only, real OAuth access (not the webhook stub above) for Calendar events and Gmail. It reuses the dashboard server for the OAuth redirect, so `DASHBOARD_PUBLIC_URL` and `DASHBOARD_TOKEN` must already be set.

1. In [Google Cloud Console](https://console.cloud.google.com), create a project, enable the **Google Calendar API** and **Gmail API**, then create an **OAuth client ID** (Application type: **Web application**).
2. Set the OAuth consent screen to **Internal** if Tara Angkor is on Google Workspace — this skips Google's public app-verification review entirely.
3. Add this exact redirect URI to the OAuth client: `<DASHBOARD_PUBLIC_URL>/oauth/google/callback`.
4. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_OAUTH_REDIRECT_URI` (the same URL from step 3) in `.env`.
5. As the owner, in the private bot chat: `/connectgoogle` sends a one-time Google sign-in link; `/googlestatus` and `/disconnectgoogle` check or revoke the connection.

Once connected, `/calendar` lists upcoming events and `/inbox` summarizes unread Gmail. The existing `/event` and `/email` draft-and-approve commands automatically switch from the generic webhook stub to the real Google APIs — no change to that workflow. If Google isn't connected, those two commands keep using `CALENDAR_WEBHOOK_URL`/`EMAIL_WEBHOOK_URL` exactly as before.

### Situations

The priority classifier scores each message on its own; `/situations` (also reachable from **⋯ More**) goes one level up and fuses related messages into one tracked issue instead — five messages about the same broken AC become one situation with a status, not five unrelated P1 alerts. Only messages already classified P0–P2 with a situation-worthy type (`task`, `blocker`, `waiting`, `decision`, `update`) are considered; chatter, questions, and P3 noise never create or touch one.

For each incoming eligible message, `app/priority/situations.py`'s `SituationLinker` checks the chat's currently open situations and decides whether the message **continues** one (updating its status, current action, dependency, and guest-affected flag in place), **resolves** one, or **starts a new one** — costing one extra AI call only when there is something to disambiguate against; a chat with no open situation gets a free "new" decision with no model call at all. If the AI call fails, a keyword-overlap heuristic decides instead, so a provider outage never blocks fusion, only makes it cruder. A situation can never be pointed at an ID the model didn't actually see, and its full source message history (last 20 links) stays attached for traceability. Tap into a situation from the list to see its full detail and mark it resolved.

When a chat reports the same kind of problem (2+ shared keywords) at least 3 times within 7 days, `detect_repeated_issue()` sends a one-time **⚠️ REPEATED ISSUE** alert to the owner — it fires once on crossing the threshold, not again on every later occurrence, so a recurring problem doesn't turn into recurring noise.

### Management briefs and to-do (`/brief`, `/today`, `/yesterday`, `/week`, `/month`)

`app/priority/briefs.py`'s `BriefEngine` turns "what happened today/yesterday/this week/this month?" and "what do I need to do?" into one structured answer built on Situations, not a fresh summarization pass every time. `/brief` (also **📋 Brief** on the main menu) is the proactive snapshot — currently critical (🔴) and pending (🟠) situations plus pending approval drafts (📌 Your Actions) — while `/today`, `/yesterday`, `/week`, and `/month` are retrospectives over situations active in that window, including ones resolved during it. The same engine is registered as the `get_management_brief` agent tool, so the conversational assistant can answer free-form phrasings of the same question directly.

### Document Intelligence (PDF, Excel, Word, images)

`app/content/extract.py`'s `DocumentExtractor` now also reads `.docx` (via the optional `python-docx` package) and runs OCR on images and scanned documents (via the optional `pytesseract` + Pillow, backed by the system Tesseract binary — already installed in the Docker image). OCR'd text flows through the same text-context path as a PDF; a photo with no readable text still gets a clear "no text found" result rather than a fabricated summary. Attach a file to a message in the owner's private chat (or an approved group) and ask a question about it, the same way `/web` and the existing file-upload flow already work.

### Weekly meeting deck (`/weeklyreport`)

Owner-only, private chat: `/weeklyreport` (or "prepare this week's meeting presentation") builds a 9-slide `.pptx` from the last 7 days of Situations and pending approvals — Overview, Department Status, Major Issues, Resolved, Pending, Management Decisions, Guest/Customer Issues, Follow-Up Actions, and Recommendations — and sends it back as a file. "Department" is approximated from each situation's responsible party or source chat, since Situations don't carry a formal department link yet. The Recommendations slide is explicitly labeled as heuristic observations, not verified facts, kept separate from the factual slides ahead of it.
