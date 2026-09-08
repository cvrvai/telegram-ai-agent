# 🛠️ Telegram Priority Assistant - Complete Setup & Operations Guide

This guide walks you through setting up, configuring, and operating the **Telegram Priority AI Assistant** with its dual Telethon Userbot and Interactive Bot Assistant, MongoDB storage, Ollama AI reasoning, and Google Calendar / Google Meet integrations.

---

## 🏗️ Architecture Overview

The system runs two concurrent Telegram clients inside a single daemon process (`main.py run`):
1. **Personal Userbot (`Telethon Client`):**
   - Connects to your personal Telegram account (`runtime/telegram_ai_session.session`).
   - Monitored chats/groups are gated by explicit owner approval (zero unauthorized eavesdropping).
   - Performs outbound actions (e.g. sending approved messages to groups or contacts) on your behalf.
2. **Interactive Bot Assistant (`Telegram Bot API`):**
   - Connects to your dedicated Bot (`runtime/bot_listener_session.session`).
   - Serves as your private command center for approvals, natural language queries, chat selection, and digests.
3. **MongoDB:**
   - High-performance, durable storage for message history, classifications, focus scopes, and OAuth tokens.
4. **Ollama Cloud / Local:**
   - Contextual reasoning model (e.g., `gpt-oss:120b`) for classification, summarization, and outbound drafting.
5. **Google Calendar & Meet:**
   - Real OAuth connection for scheduling events and generating live Google Meet video links (`https://meet.google.com/...`).

---

## 📋 Prerequisites

- **Python 3.10+** (or [uv](https://github.com/astral-sh/uv))
- **MongoDB 6.0+** running locally (`mongodb://127.0.0.1:27017`) or via Docker
- A Telegram account with API credentials from [my.telegram.org](https://my.telegram.org)
- A Telegram Bot created with [@BotFather](https://t.me/BotFather)
- An Ollama API Key (from [Ollama Cloud](https://ollama.com/settings/keys)) or local Ollama instance

---

## 🚀 Step-by-Step Installation

### Step 1: Clone Repository & Create Virtual Environment

```powershell
# Navigate to project directory
cd telegrambot

# Create and activate virtual environment
python -m venv .venv
.venv\Scripts\activate

# Install dependencies
pip install -e .
```

---

### Step 2: Configure `.env` File

Copy the template:
```powershell
copy .env.example .env
```

Set your configuration values in `.env`:

```env
# 1. Telegram Client (Personal Userbot) - from https://my.telegram.org
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_PHONE=+855XXXXXXXX
TELEGRAM_SESSION_NAME=runtime/telegram_ai_session

# 2. Interactive Telegram Bot - from @BotFather
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
TELEGRAM_BOT_SESSION_NAME=runtime/bot_listener_session
OWNER_USER_ID=your_telegram_user_id
NOTIFICATION_CHAT_ID=your_telegram_user_id

# 3. Database
STORAGE_BACKEND=mongo
MONGO_URI=mongodb://127.0.0.1:27017
MONGO_DATABASE=telegram_business

# 4. AI Provider (Ollama Cloud)
AI_PROVIDER=ollama
OLLAMA_BASE_URL=https://ollama.com/v1
OLLAMA_MODEL=gpt-oss:120b
OLLAMA_API_KEY=your_ollama_api_key
AI_REQUEST_TIMEOUT_SECONDS=120

# 5. Google Calendar & Meet OAuth (Optional, for Meet link generation)
GOOGLE_CLIENT_ID=your_client_id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your_client_secret
GOOGLE_OAUTH_REDIRECT_URI=https://your-domain.com/oauth/google/callback
```

---

### Step 3: Authorize Your Personal Telegram Account (One-Time)

Before launching the background service, log in your personal userbot account:

```powershell
.venv\Scripts\python.exe scripts\login_userbot.py
```

1. Enter your phone number (if prompted).
2. Enter the verification code sent to your Telegram app.
3. If you have Two-Step Verification (2FA), enter your password.
4. The authenticated session is safely saved to `runtime/telegram_ai_session.session`.

---

### Step 4: Start MongoDB

Make sure MongoDB is running on `mongodb://127.0.0.1:27017`:
```powershell
# Using Docker
docker run -d -p 27017:27017 --name mongo mongo:latest

# Or check Windows service
Get-Service MongoDB
```

---

### Step 5: Start the Bot Daemon

Start both the Userbot and Interactive Assistant concurrently:
```powershell
.venv\Scripts\python.exe main.py run
```

You will see:
```text
✓ Telethon Userbot connected and listening to all chats!
✓ Interactive Bot Assistant started! You can now chat with your bot on Telegram.
```

---

## 📱 How to Use in Telegram

### 1. Configure Monitored Chats & Groups (`/setup`)
In your private chat with your bot assistant:
1. Send `/setup` or tap **`[🔐 Manage Bot]`** on your bottom menu.
2. Tap **`[👥 Select Groups]`** to monitor internal team groups (e.g. `AIC`).
3. Tap **`[👤 Select People]`** to monitor direct contacts (e.g. `Dai Vai`).
4. **One-Tap Toggle:** Tapping any chat turns `[➕ Chat]` into `[✅ Chat]` and **auto-saves instantly**. No separate save button required!
5. Tap **`[🔐 Revoke / Access]`** at any time to remove access with 1 click.

---

### 2. Summarize Chats & Inboxes
Ask naturally in chat:
* *"Can you summarize the recent messages from AIC group?"*
* *"What did Dai Vai say earlier today?"*
* *"Give me a brief of today's important updates"*

---

### 3. Draft & Send Outbound Messages with Safety Approval
To send a message through your personal account without leaving the bot:
1. Ask the bot:
   > *"Tell Dai Vai in AIC group that we are meeting tomorrow at 9am about the crayfish tank"*
2. The bot generates an accurate draft and presents:
   `[✅ Approve]`   `[❌ Reject]`
3. Tap **`[✅ Approve]`**:
   - The message is posted immediately into the target group by your account.
   - The confirmation displays cleanly without button clutter:
     `✅ Message sent after approval.`

---

### 4. Google Meet Video Scheduling
1. With Google Calendar connected, ask:
   > *"Schedule a Google Meet with Dai Vai tomorrow at 9am about the crayfish tank and send it to AIC group"*
2. The bot creates the Google Calendar event, generates an active Google Meet video room (`https://meet.google.com/xxx-yyyy-zzz`), and drafts the message.
3. Tap **`[✅ Approve]`** -> the message with the clickable Google Meet video preview card is sent directly to the chat!

---

## 🛠️ Maintenance & CLI Commands

| Command | Action |
| :--- | :--- |
| `python main.py run` | Start the full live daemon (Userbot + Bot Assistant + Schedulers) |
| `python main.py digest` | Trigger an immediate manual 3-tier digest |
| `python main.py stats` | Display database metrics and classification counters |
| `python main.py test` | Run local simulation tests |
| `python migrate_sqlite_to_mongo.py` | Migrate legacy SQLite records to MongoDB |
| `scripts/login_userbot.py` | Interactive Telethon login / session refresh |
| `scripts/userbot_listener.py` | Standalone userbot listener & OpenClaw bridge |

---

## 🔒 Security Best Practices
- Never commit `.env` or files in `runtime/` (`*.session` files contain active Telegram login keys).
- All outbound messages require explicit `[✅ Approve]` interaction by the owner.
- The assistant operates in a zero-trust model: conversation messages are instructions, but chat history and group messages are treated strictly as reference evidence.
