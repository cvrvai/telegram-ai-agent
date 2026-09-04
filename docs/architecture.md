# Maintainable application structure

Runtime code is grouped under `app/`; `main.py` and `migrate_sqlite_to_mongo.py` remain the two explicit entry points. New business behavior belongs under `app/` and should not call Telegram APIs directly.

```text
app/
  agent/       bounded decision runtime, policy, context, and tools
  core/        shared message and digest schemas
  priority/    filtering, classification, and digest engines
  telegram/    notification and scheduling adapters
  ai/          provider interface and model adapters
  assistants/  profiles and assistant routing
  content/     bounded document/image extraction
  dashboard/   authenticated Telegram Web App and management APIs
  security/    users, groups, pairing, and authorization policy
  services/    business use cases that coordinate the pieces above
  storage/     MongoDB repositories (SQLite is migration-only)
  usage/       budget admission, settlement, and usage records
main.py        Telegram event transport and CLI entry point
migrate_sqlite_to_mongo.py  one-time migration utility
```

Keep this dependency direction:

```text
Telegram or dashboard transport -> services -> ai/storage/security/usage
```

AI providers never decide who may read a record. Storage never sends a Telegram message. A new provider implements `AIProvider` and returns token usage with its model rates; the assistant service performs authorization, budget admission, persistence, and usage recording around it.

The current implementation includes persistent assistant conversations and turns, approved-user/group policy, budget enforcement, Ollama local/Cloud support, department profiles, bounded document handling, business tools, approval records, Mongo migration, and a Telegram Web App management surface.
