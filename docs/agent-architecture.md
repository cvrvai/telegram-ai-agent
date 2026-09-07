# Work management agent

The Telegram assistant uses a bounded, provider-neutral agent runtime for ordinary natural-language messages. Slash commands and existing inline-button workflows remain in the Telegram handler.

## Boundaries

- `AgentDecision` is the only model output accepted by the runtime. It can be a final answer, a clarification, or one allow-listed tool call.
- `ToolRegistry` contains typed input/output schemas and risk metadata. The model cannot call repository methods or arbitrary Python functions.
- `ContextRetriever` loads a small recent conversation window, open work, projects, saved memory, and messages from the owner's explicitly selected Telegram chat IDs.
- `PolicyEngine` denies unapproved users, speculative writes, unresolved writes, and approval-required tools. Safe writes require an explicit command.
- The runtime executes at most four tool rounds, rejects repeated calls, and stores only the final user/assistant turn. Tool decisions are recorded in `agent_audit_events` without hidden reasoning.

## Initial tool surface

Read tools cover projects, work items, due-soon work, focused source-message search, and user memory. Safe writes cover status, priority, due date, comments, and assignment to an approved user. Creation, deletion, inventory, sales, purchasing, notifications, and external sends are intentionally deferred.

Production starts with MongoDB (`STORAGE_BACKEND=mongo` and `MONGO_URI`). SQLite remains a migration and test compatibility path; the agent never writes directly to either storage adapter.
