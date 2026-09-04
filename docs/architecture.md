# Maintainable application structure

The current top-level Python modules remain the compatibility layer for the existing CLI. New business behavior belongs under `app/` and should not call Telegram APIs directly.

```text
app/
  ai/          provider interface and model adapters
  assistants/  profiles and assistant routing
  content/     bounded document/image extraction
  dashboard/   authenticated Telegram Web App and management APIs
  security/    users, groups, pairing, and authorization policy
  services/    business use cases that coordinate the pieces above
  storage/     MongoDB repositories (SQLite is migration-only)
  usage/       budget admission, settlement, and usage records
main.py        Telegram event transport and legacy CLI entry point
classifier.py  legacy priority classification adapter
database.py    legacy message/digest repository plus schema bootstrap
```

Keep this dependency direction:

```text
Telegram or dashboard transport -> services -> ai/storage/security/usage
```

AI providers never decide who may read a record. Storage never sends a Telegram message. A new provider implements `AIProvider` and returns token usage with its model rates; the assistant service performs authorization, budget admission, persistence, and usage recording around it.

The current implementation includes persistent assistant conversations and turns, approved-user/group policy, budget enforcement, Gemini/Ollama support, department profiles, bounded document handling, business tools, approval records, Mongo migration, and a Telegram Web App management surface.
