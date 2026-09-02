# 🤖 Telegram AI Priority Filter & Digest Bot

> **An intelligent, context-aware Telegram notification gatekeeper and periodic digest assistant powered by Google Gemini and Ollama.**

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
- 📱 **Interactive Telegram Dashboard:** Chat directly with your bot in Telegram with one-tap buttons:
  - `[ 📋 Generate Digest Now ]`
  - `[ 🔴 Priority Messages ]`
  - `[ 📊 System Stats ]`
- 💬 **Ask AI About Your Chats:** Type any question to your bot (e.g. *"What did my lecturer announce today?"* or *"Any updates on CloudKH?"*) and get instant answers based on captured message context.
- 🔌 **Multi-Model Support:** Native integration with **Google Gemini** (`gemini-3.5-flash-lite`, `gemini-3.6-flash`) and local/remote **Ollama**.

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

#### C. AI API Key (Google Gemini - Free)
1. Get a free API key from [Google AI Studio](https://aistudio.google.com).
2. (Optional: You can also use local [Ollama](https://ollama.com)).

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

# 3. AI Provider (Google Gemini)
AI_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.5-flash-lite

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

### 2. Chat with Your Bot on Telegram
Open your bot in Telegram and send:
```text
/start
```
Use the interactive inline buttons or ask questions conversationally!

---

## 🛠️ CLI Commands

| Command | Description |
| :--- | :--- |
| `python main.py run` | Start the live Userbot listener, interactive bot, and digest scheduler |
| `python main.py test` | Run the simulation test suite on sample messages |
| `python main.py digest` | Trigger an immediate manual digest of pending messages |
| `python main.py stats` | View database statistics (total messages, noise filtered, pending queue) |

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
├── models.py              # Pydantic schemas (Classification, Messages, Digests)
├── database.py            # Async SQLite database engine
├── prefilter.py           # Zero-token rule pre-filter
├── classifier.py          # AI priority classifier (Gemini / Ollama / OpenAI)
├── notifier.py            # Telegram alert dispatcher & HTML formatter
├── digest.py              # 3-Tier periodic digest aggregation engine
├── scheduler.py           # APScheduler background digest timer
├── main.py                # Main CLI daemon & interactive Telegram Bot
├── test_classifier.py     # Comprehensive test suite
└── pyproject.toml         # Dependencies and metadata
```
