# AI running costs: Telegram business assistant

Updated September 3, 2026. Currency: USD. This document covers AI usage for the [business assistant build plan](client-video-feature-scope.md). It excludes development, human support, tax, and separately charged integrations. The figures are planning calculations, not measured usage or guaranteed chat quotas.

## Proposed starting allowance

Start a light-use pilot with a **$20/month AI API allowance**, shared across all users, groups, assistants, and automatic AI activity. This is a proposed spending budget; no subscription, credit purchase, or spending limit has been configured by this documentation update.

The allowance can be used by all implemented AI features. It does not unlock a feature tier or guarantee a full month of use. Check quality and actual consumption during the pilot before promising a fixed service allowance.

## Model and billing assumptions

Use Claude Sonnet 5 as the initial model for a consistent baseline. Standard API rates are **$2 per million input tokens** and **$10 per million output tokens**. Input includes instructions, supplied context, and file content; output includes billable generated content and reasoning. Cache charges and paid tools can change the bill. Source: [Claude platform pricing](https://platform.claude.com/docs/en/about-claude/pricing).

A token is a unit of text processed by the AI. The allowance examples below use specified token counts so they can be checked; they are not word counts. Long histories and large documents can cost much more than a short question. A single visible answer can require several billed calls.

This plan uses API billing. A personal Claude subscription is a different product and is not included in the $20 allowance described here.

## What $20 could support

For these examples, cost = (input tokens / 1,000,000 x $2) + (output tokens / 1,000,000 x $10).

| Illustrative activity | Assumed total input / output per activity | Calculated cost | If the full $20 were spent only on this activity |
| --- | ---: | ---: | ---: |
| Brief question and reply, with limited history | 2,000 / 500 tokens | $0.009 | About 2,200 exchanges |
| Longer request and detailed reply, with more context | 10,000 / 2,000 tokens | $0.040 | About 500 exchanges |
| Classify one incoming message | 1,000 / 150 tokens | $0.0035 | About 5,700 classifications |

An exchange means one user question and one AI answer, not a complete conversation. These alternatives cannot be added together. The assumptions include all model input/output for that activity; extra calls, larger context, reasoning, and retries reduce capacity. These calculations exclude caching adjustments, paid tools, and tax.

### Example of mixed use

Using the exact assumptions above:

| Monthly activity | Cost |
| --- | ---: |
| 1,000 brief question/reply exchanges | $9.00 |
| 100 longer question/reply exchanges | $4.00 |
| 1,000 message classifications | $3.50 |
| **Total** | **$16.50** |
| **Remaining allowance** | **$3.50** |

The remaining $3.50 would need to cover any additional AI summaries, document analysis, retries, and other calls. This is an illustration, not a prediction of this business's workload. For comparison, 10,000 classifications at the stated size alone would cost $35, so a busy monitored group can exceed $20 even with little direct chat.

## How each feature uses the allowance

| Feature | What consumes AI credit |
| --- | --- |
| Private and group chat | Reading instructions and relevant context, then generating replies. Each follow-up can create another charge. |
| Important-message alerts | AI classification of messages that pass the rules/noise filter. Sending a saved alert is not another model call by itself. |
| Daily and requested summaries | Newly generated summaries consume credit. Formatting stored classifications into a digest can avoid an additional AI call. |
| Memory | Storing/retrieving local records does not inherently require AI. Reading retrieved context and generating memory summaries do. |
| Documents and images | Reading supplied content and producing an answer. Size, number of pages/images, and processing steps affect usage. |
| Separate assistants | Each assistant's calls draw from the same shared allowance. Creating a profile does not create extra AI credit. |
| Access, approval buttons, and dashboard | Routine permission checks, settings, and displaying saved records need no model call. AI-generated drafts and explanations still consume credit. |

The number of approved users or bots does not establish a fixed model bill. Their combined activity does.

## Does $20 support all features well?

All listed features can be implemented without a higher AI subscription tier. The $20 allowance is suitable as a bounded trial, not proof of adequate capacity for a busy team. Available business context, document readability, model performance, and the implementation affect answer quality.

Use representative business questions, urgent messages, and documents to evaluate usefulness. Measure direct replies and background activity separately. Increase the allowance only when observed workload justifies it; no automatic upgrade is part of this proposal.

## Spending controls to implement

1. Track actual provider usage and estimated spending by assistant, feature, and budget period.
2. Warn at 80% of the allowance and reserve estimated request cost before dispatch, including concurrent work.
3. Pause new AI calls when the remaining allowance cannot cover the request. Do not switch silently to another paid provider.
4. Keep permitted history, existing summaries, settings, and usage records available while AI is paused. Show queued or skipped AI work clearly.
5. Reconcile estimates with provider billing and review the pilot. Do not promise an exact invoice cap solely from delayed usage reporting.

These controls are planned features. They are not present in the current application.

## Other APIs

The initial business scope does not require a second paid AI provider. Existing Gemini/Ollama integrations may remain available in the code, but this cost baseline assumes Sonnet usage only. A different provider or model needs its own measured estimate.

Paid web search, voice services, and external business integrations are outside the initial scope and outside the examples above. Price them separately if added. The planned dashboard, access rules, routing, and approval controls do not inherently require a separate paid AI API.

## Short message for the boss

We can start with $20 per month in shared AI usage credit. At the example message sizes, that could cover roughly 2,200 brief question-and-reply exchanges or 500 longer exchanges if used only for chat. Automatic message checks, summaries, and document analysis use the same allowance, so direct-chat capacity will be lower when those features are active. The planned features can all use this budget, but we should test real business usage before promising a monthly capacity. We will include spending visibility and a control to pause new AI work when the allowance is used.
