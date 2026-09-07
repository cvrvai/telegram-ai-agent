# Build plan: Telegram business assistant

Updated September 4, 2026. Status: implementation slice completed and verified with automated tests. External provider credentials and production deployment remain configuration steps.

Build an assistant for the boss and approved staff to ask business questions, follow important conversations, review documents, and receive useful summaries through Telegram. Include a management dashboard and a shared AI spending allowance, initially proposed at $20 per month.

The [reference video](https://www.youtube.com/watch?v=z3NhRgiItwQ) informs the Telegram interaction and management features. The user's latest business requirements define this plan. Software development and project-file execution from the video are outside the business feature list.

## Features we will build

| Feature | Business behavior | Existing foundation | Work required |
| --- | --- | --- | --- |
| 1. Private AI chat | Answer business questions, draft replies, and explain supplied information. | Persistent scoped conversations, history search, Ollama local/Cloud provider, and budget controls. | Configure the Ollama endpoint, model, and key. |
| 2. Group assistance | Respond to mentions, replies, and configured trigger phrases in approved groups. Keep routine group conversation quiet unless a response rule applies. | Group ingestion exists; interactive answers are private-only. | Add group activation, sender permissions, trigger settings, and replies to the correct conversation. |
| 3. Important-message alerts | Flag urgent requests, deadlines, and items needing attention, with a short reason and a source link where available. | Priority classifier, noise filter, and notifications exist. | Replace personal/university defaults with configurable business rules; select monitored sources and authorized alert recipients. |
| 4. Scheduled and on-demand summaries | Present decisions, pending requests, action items, and stated deadlines from selected conversations. Let the administrator set times and destinations. | Scheduled/manual digests assemble stored classifications. | Add summaries across messages where useful; scope results by recipient and distinguish stated facts from inference. Preserve a low-cost digest assembled from existing records. |
| 5. Conversation memory | Recall relevant saved discussions and continue an earlier conversation without exposing another department's information. | Incoming messages and digests are stored in SQLite. | Store assistant conversations, retrieve relevant records, and add retention, reset, and deletion controls. Historical coverage starts with captured messages or explicitly imported material. |
| 6. Document and image assistance | Summarize shared documents and extract readable details from images, with file or page references where possible. | PDF, TXT, CSV, JSON, YAML, XLSX, XLS, and common image inputs with bounded extraction. | Provider capability and credentials determine image/PDF quality. |
| 7. Separate assistants | Provide separate department/project assistants, such as sales or operations, with their own instructions and history. | `ASSISTANT_IDS` profiles and `/assistant ID` routing provide independent contexts. | Add separate bot identities and per-assistant source rules when required. |
| 8. Access and approvals | Permit approved users and groups only; let administrators approve access and review actions needing confirmation. | Interactive handlers lack a sender allowlist. | Add pairing requests, approve/deny/revoke controls, roles, and an approval record. Initial proposed business action: preview and approve sharing a prepared summary into another approved group. |
| 9. Management dashboard | Manage projects, tasks, assistants, users, groups, schedules, permissions, approvals, and AI spending. | Telegram Web App with authenticated APIs and CPM project-plan views. | Extend styling and deploy behind HTTPS/authentication for production. |

## Interaction details

- Provide typing/progress feedback, acknowledgement reactions where supported, replies linked to the original message, and readable splitting of long responses.
- Offer clear menus for help, summaries, important messages, assistant selection, starting a new conversation, and viewing usage. Administrative controls require an administrator.
- Access modes: approval required for new users, approved-users-only, and fully disabled. Disabling an assistant stops both private and group AI handling.
- A new conversation resets the active discussion; deleting saved memory is a separate, explicit action.
- Keep a reviewed message's exact text and destination attached to its approval. Denial, expiry, or a changed draft must prevent sending; repeated button presses must not send twice.
- Scheduled summaries use destinations explicitly configured by an administrator. This standing permission is separate from approval of an ad hoc message to another group.

## What the assistant can read

The business deployment must explicitly select its source chats and recipients. A Telegram bot receives messages delivered to that bot; it does not automatically gain access to someone's private inbox or arbitrary past conversations. Group delivery depends on Telegram settings. See the [Telegram Bot FAQ](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).

This repository also has a Telethon listener connected to a user account. Retain that capability only for an account the owner deliberately connects and for approved source chats. Keep the owner's private sources separate from shared business sources. Existing local messages must not automatically become visible to newly paired staff.

Every history query, summary, attachment, notification, and dashboard view must enforce the requesting user's source permissions. Uploaded documents and chat messages provide information; they cannot grant permissions or approve an action.

## AI budget and feature availability

Use $20/month as the proposed initial allowance for a light-use pilot. It is one shared allowance for all assistants, users, groups, classifications, summaries, document analysis, and retries. It is usage-based API spending, not a fixed-message subscription.

The same implemented features may be enabled at this allowance. Their frequency and workload determine whether it lasts a month. Accuracy and usefulness must be checked on representative business messages and documents. A larger allowance permits more usage; it does not establish quality by itself.

See [AI running costs](client-running-costs.md) for current rates, the assumptions behind the approximately 2,200 brief or 500 longer exchange examples, and a mixed-use calculation. Those alternatives cannot be added together or promised as a quota.

Planned controls:

- Record actual provider usage and estimated cost for every AI operation, attributed to its assistant and feature. Use one configured budget period and timezone.
- Warn at 80% of the allowance. Before dispatch, account for in-flight work and reserve an estimated maximum request cost; reconcile with actual usage afterward.
- Pause new AI requests when the remaining allowance cannot cover them. Do not silently switch to another paid provider or a more expensive model.
- Reconcile application estimates with provider billing. Price changes, delayed records, and in-flight work mean the dashboard must not advertise an exact invoice guarantee.
- While AI is paused, keep authorized access to saved history, existing summaries, settings, and usage records. Show which new summaries or alerts are waiting for AI; do not claim they are still being generated normally.
- Use the existing noise filter, bounded context, output limits, and controlled retries to reduce spending. Simple menus and viewing saved data do not require an AI call.

## Repository decision: extend the current project

Recommended base: this existing checkout of [telegram-ai-priority-bot](https://github.com/cvrvai/telegram-ai-priority-bot). It already contains the ingestion, classification, storage, alerts, digests, and Telegram menu foundations. Continue development here on a feature branch; another clone of the same project is unnecessary.

For this business scope, add a direct Claude API integration and explicit application workflows. A continuously running coding-agent session is not a prerequisite for the listed business features.

| Candidate | Assessment | Decision |
| --- | --- | --- |
| Current Python project | Existing priority and digest functionality directly supports the agreed business scope. Authorization, scoped data access, and provider consistency need work before staff access. | Extend it and refactor incrementally. |
| Official Telegram plugin | Connects Telegram to a running Claude Code session using a Bun-based MCP server. Useful references include pairing, delivery, and multiple bot state directories. It does not supply this application's durable business history. | Use as a behavior reference; evaluate individual components only where they fit. Cloning it alone would not deliver the business product. |
| Creator's dashboard project | The video's [resource package](https://jannismoore.gumroad.com/l/kubdb) was identified previously, but its delivered GitHub source, dependencies, and reuse license have not been verified. | Treat reuse as an optional shortcut after source inspection. The build must be able to proceed with our own dashboard. |

The [official plugin README](https://github.com/anthropics/claude-plugins-official/blob/main/external_plugins/telegram/README.md) documents its integration. Its repository has a [root Apache 2.0 license](https://github.com/anthropics/claude-plugins-official/blob/main/LICENSE); check the applicable component notices before copying code. That license does not establish permission to reuse the creator's separate dashboard.

If the creator's source becomes available, inspect it in a separate directory, review its license and dependencies, and compare it with this plan before adopting code. Do not replace the current repository or run an unfamiliar installer as part of that inspection.

## Existing code to reuse and adjust

These findings come from static inspection, not a live integration test.

| Area | Files | Required change |
| --- | --- | --- |
| Message intake and interaction | `main.py` | Separate intake, commands, conversations, and callbacks. Check identity and permissions before fetching data or performing actions. Add group routing and per-bot state. |
| Priority and answer generation | `app/priority/classifier.py`, `app/priority/prefilter.py` | Preserve filtering/classification; add a shared model interface for classification, chat, documents, and summaries with usage reporting. Current Q&A takes a different provider path from classification. |
| Storage | `app/storage/sqlite_messages.py`, `app/core/models.py` | Migrate existing records safely; add users, assistants, memberships, scoped conversations, files, approval records, and AI usage. Introduce scoped retrieval instead of global recent-message queries. |
| Alerts and digests | `app/telegram/notifier.py`, `app/priority/digest.py`, `app/telegram/scheduler.py` | Apply recipient/source permissions, configurable destinations, and delivery tracking. Mark scheduled delivery complete only after successful delivery. |
| Configuration | `config.py`, `user_profile.yaml` | Add business profiles and validated assistant/group/budget settings. Keep secrets outside public responses and dashboard output. |
| Administration | New dashboard and management routes | Use the same authorization and settings services as Telegram; show runtime status separately from configured credentials. |

The current Q&A handler reads the latest 35 captured messages globally. Both message and callback handlers need access checks before adding staff. Substring shortcuts such as `task` and `update` also need explicit command routing so ordinary business requests reach the assistant.

## Delivery phases and acceptance

The core application slice is complete. The remaining checklist items are production hardening or provider-specific configuration.

### Phase 1: access, business chat, and spending controls

- [ ] Add administrator setup, approved-user access, source permissions, and scoped database migration.
- [ ] Add Claude support through the shared model interface, conversation persistence, explicit menus, and useful failure messages.
- [ ] Add operation-level usage records, budget warnings, and request admission checks.
- [ ] Preserve priority filtering and existing digests with recipient checks.
- Acceptance: an approved user can chat and view permitted messages; an unapproved user cannot retrieve data through messages or callbacks. A normal question containing "task" reaches chat. A mocked exhausted allowance stops new AI calls without exposing private context in errors.

### Phase 2: groups, memory, and business summaries

- [ ] Add approved-group activation, mentions/replies/custom triggers, and delivery preferences.
- [ ] Retrieve relevant saved discussions with source links and user/assistant isolation.
- [ ] Add configurable summary schedules and destinations, decisions/action items, and delivery recovery.
- Acceptance: selected group triggers produce one correctly threaded reply; routine conversation stays quiet. A summary includes only authorized sources. Restarting preserves history and schedules, and a failed delivery is not recorded as successful.

### Phase 3: documents and separate assistants

- [ ] Add the listed file formats, extraction limits, references, and understandable unreadable-file responses.
- [ ] Add department/project profiles, assistant selection, and separate bot identities where configured.
- [ ] Meter all assistants against the shared allowance.
- Acceptance: a representative business PDF and image produce useful summaries without invented unreadable details. Two assistants retain separate context, and neither can retrieve the other's restricted material.

### Phase 4: dashboard and approvals

- [ ] Build authenticated controls for assistants, users, groups, schedules, history, delivery settings, and usage.
- [ ] Add pairing approval/revocation and the proposed summary-sharing approval workflow.
- [ ] Show connected, busy, disconnected, error, and budget-paused states based on actual runtime state.
- Acceptance: only an administrator can change access or approve an action. Denied, expired, changed, or repeated approvals cannot cause a send. Dashboard settings persist and affect Telegram behavior.

### Phase 5: Docker packaging and business pilot

- [ ] Add Docker packaging and startup documentation for the user's existing deployment setup.
- [ ] Persist application data, configured Telegram sessions, and supported uploaded files; provide health checks and backup/restore instructions.
- [ ] Run a bounded pilot with approved business examples and measure answer usefulness, missed/incorrect priority flags, summary quality, and cost by feature.
- Acceptance: recreation preserves permitted state; recovery works; measured usage supports an honest allowance recommendation. Production access requires successful authorization, isolation, approval, and budget checks.

## Scope boundaries

The first business release includes the nine features above. Coding tasks, arbitrary command execution, autonomous bot-to-bot conversations, voice calls, image generation, and unlisted integrations are not included. Connecting a CRM, email account, accounting system, or payment service requires its own defined workflow and cost assessment. Separate assistants do not imply automatic cooperation between them.

The dashboard will provide the agreed controls; an exact visual reproduction of the creator's dashboard has not been specified. No application code, account configuration, external messaging, purchase, or deployment was performed by this documentation update.

## Reference mapping

The original assessment used the video's complete auto-generated transcript and description; it did not verify every screen visually. Relevant business interaction references remain:

- [06:19](https://www.youtube.com/watch?v=z3NhRgiItwQ&t=379s): pairing and approved access.
- [08:23](https://www.youtube.com/watch?v=z3NhRgiItwQ&t=503s): private interaction.
- [11:01](https://www.youtube.com/watch?v=z3NhRgiItwQ&t=661s) and [15:35](https://www.youtube.com/watch?v=z3NhRgiItwQ&t=935s): group activation and triggers.
- [16:51](https://www.youtube.com/watch?v=z3NhRgiItwQ&t=1011s): access policies.
- [18:08](https://www.youtube.com/watch?v=z3NhRgiItwQ&t=1088s): delivery controls.
- [18:48](https://www.youtube.com/watch?v=z3NhRgiItwQ&t=1128s) and [19:40](https://www.youtube.com/watch?v=z3NhRgiItwQ&t=1180s): approval and memory discussions; these are explicit application deliverables in this plan.
- [20:10](https://www.youtube.com/watch?v=z3NhRgiItwQ&t=1210s): management dashboard.
