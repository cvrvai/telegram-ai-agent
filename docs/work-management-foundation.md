# Work management foundation

The assistant keeps one work-item system in `assistant_tasks`. Existing task
commands remain compatibility wrappers while new records use human project keys
(`REDCRAWSAL-12`), independent priority (`P0`–`P3`), type, parent, source, and
workflow status fields.

SQLite migrations remain available only for importing an older pilot database.
The production runtime is MongoDB-only. Existing messages keep their priority
and receive `message_type=unknown`; no historical P2/P3 values are reclassified.

Mongo uses the same document fields and unique project/display keys. New project
sequences are allocated from `next_sequence`, rather than counting records.
Telegram callbacks for new project views use the `project:<view>:<id>` route
format. Legacy callbacks and commands are still accepted.

The current Telegram Home exposes only stored features: Projects, My Work, Due
Soon, Digest, and More. Inbox, search, knowledge, reports redesign, and
inventory/sales automation remain deferred until the next approved phase.
