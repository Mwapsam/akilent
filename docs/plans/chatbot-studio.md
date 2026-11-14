# Chatbot Studio — spec and phased plan

Status (2026-11-14): **P0 and P1 committed; P2–P6 built** (uncommitted). 211 chatbot tests
pass, including the release-gate journey (§11 item 1). **Not released:** releasing is the
owner's step (checklist below).

P6 as built (release preparation):
- **Plan feature `website_chat`** (Channels group, `apps/billing/features.py`). The owner chose
  "only plans I tick": it is not in `CHANNEL_FEATURE`, so new plans don't start with it, and no
  migration adds it to existing plans. Tick it per plan in Operator Console → Plans → Features,
  or grant it per business. Plan cards and pricing hide it until `chatbot` is released
  (`channels.FEATURE_RELEASE_DEPS`). No cap on the number of bots (owner's choice); cost is
  bounded by each bot's daily AI limit and the plan's AI allowance.
- **Gate:** every chatbot page needs the feature (`module_required("website_chat")`, else the
  locked page). A new widget chat needs it too (`init` refuses with the usual "Invalid or
  inactive chatbot key."); a chat already under way carries on, so a plan change never cuts a
  visitor off. System (platform) chatbots aren't gated.
- **Public and help pages, all hidden until released:** help guide `/help/website-chat/`
  (new "Website chat" category; `Article.release`), a Website chat section on
  `/docs/channels/` (no REST endpoint; contacts come back with `source: website_chat`), and
  `/integrations/website-chat/` (registered in `marketing_pages.py`, gated on
  `marketing_channels()["website_chat"]`, checked by the claims test).
- **Messaging guide:** §4 has a "Built, not released" row for the chatbot with the wording
  rules, and §10 has five claim rows (`website-chat`, `-handoff`, `-starters`, `-results`,
  `-install`) with evidence.
- **Wording:** the "any channel" trigger now reads "When a customer messages you on WhatsApp,
  Instagram or Facebook" (website chat has its own triggers), and the list step says "(not on
  Instagram or Facebook)" instead of "(WhatsApp only)".
- **Still deferred:** the `apps/core/channels.py` registry entry. The registry is for channels
  with an env switch and a provider (Meta, SES); website chat is gated by the `chatbot` release
  and the plan feature instead, so signup and onboarding don't offer it as a channel. Revisit
  if website chat should appear in signup's channel picker. Also deferred: the widget ignores
  Enter while the previous reply is loading (the text stays in the box), and the privacy page
  doesn't mention website chat yet (a legal-text decision; the help guide says what's stored).

**Release checklist (owner):**
1. Commit and deploy. Migrations: chatbot 0006–0009 (0006 is in an earlier commit).
2. Make sure `chat-widget/v1/chat.js` is on the static host (collectstatic/CloudFront) and
   the beat schedule includes `purge_idle_chat_sessions`.
3. Tick `website_chat` on the plans that should sell it (or grant it to the pilot business).
4. Staging: check AI limits and fallbacks (§11 item 3), and the widget on a real third-party
   site in desktop and mobile Safari and Chrome (§11 item 2).
5. Add `chatbot` to `RELEASED_FEATURES` in the server's `.env`, restart, then change the §4 row
   in `docs/marketing/messaging.md` to **Live, depending on the plan**.
6. One pilot business runs it on its live site and gets at least one real lead (§11 item 4).

P5 as built:
- **Hand-off** (`services/handoff.py`, replaces the never-wired `perform_handoff` /
  `handoff_action`; the field and `ChatbotAction` table stay, unused):
  - The visitor asks: the widget's **"Talk to a person"** header button (`/api/chat/handoff/`)
    or a message like "can I talk to a human". The conversation's automations pause until a
    teammate resumes them, so neither the bot nor its AI answers over the team. The bot's open
    question is withdrawn. The visitor gets "I've asked the team to join…" once.
  - The bot can't answer (AI unsure, off, daily limit, chat's AI turns used): the one-time
    holding reply as before. The bot keeps answering what it can.
  - Either way, the chat's first hand-off adds an inbox note ("Website chat handed to your team:
    <reason>", a SYSTEM message never shown to the visitor), marks it unread and emails the
    assigned teammate or the owners (Celery `notify_handoff`, once per chat). A later visitor
    request after a bot hand-off adds a second note but no second email.
    `ChatSession.handoff_at` / `handoff_reason` (migration 0009). Visitor messages the AI
    couldn't answer get `metadata.chatbot_unanswered`.
- **Widget state with every response** (`conversation.chat_state`): `expect`, `handoff`
  (`{"asks_details"}` while no teammate has replied since the hand-off) and `ended`. The widget
  shows the "a person will reply here" card, with the name/email form when we can't reach the
  visitor, and "The team closed this chat…" when the conversation is closed (writing re-opens
  it). The launcher is now a keyboard-reachable button with a label.
- **Analytics** (`/chatbot/<pk>/analytics/`): the funnel (loaded → opened → wrote → left
  details → lead → qualified → paid), paid revenue by currency from orders linked to the chat's
  conversation, hand-offs (count, reasons, typical first reply from a person, how many became
  leads or paid) and the latest questions the AI couldn't answer, with links to the chat and
  to Knowledge. "Step by step" links to the automation's stats for drop-off per step. The old
  action and ticket counts are gone from the page (the functions stay for their tests).
- **Not changed, noted:** the inbox's "Replied" badge counts any outbound message, including
  the bot's holding or hand-off message (the same on every channel today). A handed-over chat
  is still unread, paused, noted and emailed, but the badge can read "Replied". Changing it
  touches shared reply metrics, so it needs its own decision.
- **Confirmation card:** the hand-off card above. A general "card" message type for flows was
  not built; `reply_text` covers confirmations for now.

P4 as built (an owner goes from "New chatbot" to a live bot without the automation editor):
- **New chatbot** asks for a name, a starter and (optionally) the website. `apps/chatbot/starters.py`
  has three starters: *Answer questions, hand over when stuck* (`ai_answer` on every message),
  *Capture sales leads* (greets with buttons on open, `create_lead`, two lead questions, tells the
  team) and *Help choose a product* (`lookup_product`, then AI, then the team). Each is saved as
  the bot's own Workflow, **unpublished**, with the fields its questions need. All three pass the
  publish checks as is (tested).
- **Bot page:** a "Get it live" checklist (conversation chosen → turned on → website added → code
  seen on the site), a Conversation card with *Turn it on* / *Put changes live* (owner/admin only,
  `automation_api.publish`), and the Install card.
- **Install check:** `init` stamps the allowed origin on `ChatbotConfig.seen_on` (migration 0008, at
  most hourly; Akilent's own origin ignored). A refused origin is kept in the cache (5 per bot,
  7 days) and shown as "tried to load on www.example.com" with *Add this website*
  (`services/install.py`).
- **Try it (live preview):** `/chatbot/<pk>/preview/`, a chat window in the bot's colour that
  plays the saved draft through `apps.automation.simulate` (nothing sent or saved). Taps, typed
  answers, a "behind the scenes" toggle, and a note where the real bot's AI would answer. The
  automation editor links to it ("Try in chat") and back to the bot when the workflow belongs to
  a released chatbot. It is a separate page, not beside the editor: the editor is already full,
  and the preview uses saved changes.
- **Settings:** a Conversation picker (any website-chat automation, or none = AI only) replaces
  Purpose. The list shows websites, setup progress, and chats and leads in the last 7 days.
- **Engine fix:** `lookup_product` with no `query_variable` (the editor never offers one) searched
  for "" and never found anything. It now searches the customer's message.
- **Simulator fix:** `{contact}` is filled in previews, as the engine does.
- **Not done / noted:**
  - The bot's workflow counts towards the plan's automation limit, and the editor needs the
    `automations` feature; the plan feature placement is P6.
  - Widget: Enter is ignored while the previous reply is loading (the text stays in the box). P6
    widget polish, with the launcher's missing role/label.
  - Templates use "Get a quote" wording; owners edit it in the automation editor.

P3 as built:
- **The bot's built-in AI is `apps.ai.api.grounded_answer`,** the checked answer the inbox's
  automatic replies use. It answers only from the business's own facts and runs the same checks
  as automatic replies; when a check fails, the chat goes to the team with a one-time holding
  reply. The custom prompt and per-bot knowledge retrieval are gone (`services/context.py`
  deleted). `services/knowledge.py` stays only because an AI ranking test compares against it.
- **It never takes actions.** The `[ACTION:slug]` marker protocol is removed, and so are the
  owner pages for free-typed action slugs and the per-bot knowledge toggles (views, URLs,
  `action_edit.html`). The `ChatbotAction` and `ChatbotKnowledgeSource` models and tables remain,
  unused at runtime; P5 decides what `handoff_action` becomes.
- **When the bot's AI doesn't answer:** the visitor asks for a person (whole words, so "agency"
  doesn't count); AI is off for the business (`is_available`); the site-wide
  `AI_AUTONOMY_ENABLED` is off; the chat has used its 40 turns; or the bot's daily limit is
  reached.
- **Per-bot daily AI limit:** `ChatbotConfig.ai_daily_limit` (default 200, 0 = no AI; migration
  0007), counted per UTC day in `services/ai_budget.py`. The bot's own AI and every AI step in its
  workflow take a turn (runs carry `chatbot_id` in their context). Keyword matches in
  `ai_classify` cost nothing. Over the limit, `ai_answer` goes its `on_handoff` way,
  `ai_classify` its `on_unsure` way (`method: "ai_limit"`, explained in run history) and
  `ai_extract` its `on_missing` way. The plan's `ai_actions_day` still applies on top.
- **Bot page:** a "How it answers" card (which automation, whether AI is on and why not, today's
  AI use against the limit), and an "AI answers per day" field (0–10,000) on the edit page.
- **Kept as is:** AI steps in an owner's workflow don't check `AI_AUTONOMY_ENABLED`, the same as
  on every other channel (the step is the owner's opt-in).

P2 status: 4,591 tests across automation, conversations, instagram, facebook, ai, chatbot,
whatsapp, contacts, core and accounts passed with no failures. A browser check with real clicks
passed: buttons on open → list with descriptions → route → reply; the open question comes back
after a reload.

P2 as built:
- **Engine:** `_run_interactive_website` (`workflow_engine.py`) runs `send_buttons` / `send_list`
  in website chat. It uses the same `WorkflowInteraction` + `claim_reply` model as Instagram quick
  replies, with ids and labels from the WhatsApp builders, so WhatsApp-style `wait_for_reply`
  routes work unchanged. `send_list` is allowed in website-only workflows even when Instagram is
  connected. `ask_question` already worked through the `reply` action.
- **Telling the widget what to show:** `apps/chatbot/services/conversation.current_prompt` reads
  the open interaction and returns `expect` with every response (init, message, open, poll):
  buttons, or a list with a heading and descriptions, or a question's input. A question gets a
  number pad or date picker from its field type, Yes/No or choice taps for yes/no and choice
  fields, and an email or phone keyboard when the field key says email/phone/mobile/whatsapp.
- **Sending a tap back:** each option has an opaque `choice`. `resolve_choice` accepts it only if
  it belongs to the open question. A button routes by its `token:option`; a choice field saves its
  key while the visitor and the inbox see the label.
- **Palette:** conversation steps (reply, buttons, list, wait, question) count as usable when the
  chatbot is released and the business has a live bot.
- **Fixed while testing:**
  - (P1 bug) When the visitor typed off-script while a question was open, the existing run
    counted as having answered, so nobody replied. The AI now answers and the question stays open.
  - (widget) The options were rebuilt on every poll. That lost keyboard focus every 4 seconds and
    could land a click on the wrong row. In the first browser run, X200 was sent when X300 was
    clicked; I couldn't reproduce it after this fix.
- **Not done:**
  - `ask_question` saves only to custom fields. An "ask for email/phone" step that writes the
    contact's own email/phone fields would need a new step type (P5 candidate).
  - The chat button is a `div` with no role or label, so it isn't keyboard or screen-reader
    reachable (P6 widget polish).
  - The `send_list` palette text still says "(WhatsApp only)" until release (P6).

P1 as built, and where it departed from the plan below:
- Triggers `website_chat.opened` and `website_chat.message_received` (`CONVERSATION_TRIGGERS` in
  `workflow_engine.py`). They're hidden from the trigger picker until `chatbot` is released.
- `apps/chatbot/services/inbound.py` decides who answers, in this order: team took over → a
  waiting automation → the bot's own workflow → the bot's AI (or a one-time holding reply when AI
  is off).
- `conversations.services.record_inbound_website_message` records the message with the same
  follow-on work as other channels.
- The `reply` action has a website-chat branch (saved for the widget, `sent_by` taken from the key,
  repeated keys return the first send).
- `/api/chat/open/` and `/api/chat/poll/` (with its own rate limit). The widget polls every 4s
  while open and 20s while closed.
- Inbox shows a globe icon for website chats.
- **Pulled forward from P4:** `ChatbotConfig.workflow` (migration 0006, plus `ChatSession.opened_at`).
  The engine scoping needed it.
- **Pulled forward from P3:** the bot's AI now respects `apps.ai.api.is_available` (plan, provider,
  owner switch). Bot replies are marked `sent_by="ai"`, so reports no longer count them as a person.
- **Deferred to P6:** the `apps/core/channels.py` registry entry. About 19 modules read the
  registry (signup, onboarding, billing, docs), and an entry would show website chat to customers
  before release. Unregistered channels are never hidden from the inbox and pass
  `_require_channel_enabled`.
- The "When a customer messages you (any channel)" trigger label should say which channels before
  release, because website chat deliberately doesn't run those workflows. The website chatbot is still an unreleased feature
(`chatbot` in `apps/core/releases.py`). Each phase below needs its own go-ahead.

P0 as built:
- `apps/chatbot/domains.py`: origin normalisation.
- `api/auth.py`: CORS via `with_cors` / `allow_cors`, and `client_ip` using the last
  X-Forwarded-For hop.
- `api/views.py`: origin checked before the rate limit; `init` resumes a saved session and returns
  its history.
- `services/conversation.py`: visitor contact + conversation from the first message, routing, inbox
  unread, `identify_visitor` that never merges.
- `responder.py`: no identify wall; a ceiling of 40 AI turns per session until P3.
- `embed.py`: an absolute snippet with `data-api`.
- `tasks.purge_idle_chat_sessions` (daily beat).
- Widget: API origin from `data-api` / the script URL, resume with history, 410/429/offline states.
- Tests: `tests/test_journey.py`.

## 1. Vision

Chatbot Studio lets a business build its own website chatbot **without code**. The owner lays out
the conversation with steps, AI handles the natural-language parts, and every conversation ends up
in Akilent as a contact, a lead and an inbox thread the team can take over.

Akilent doesn't win on having more nodes or a nicer canvas. It wins by tying each chatbot to
**business outcomes**:

- Which bot, journey or answer produced qualified leads and paid orders?
- Where do visitors drop off?
- Which questions did the knowledge base fail to answer?
- Which handoffs did the team turn into sales?

Public copy follows `docs/marketing/messaging.md`. Until release, the chatbot may only be described as
"Coming soon".

## 2. Decisions

| Decision | Choice |
|---|---|
| Control model | **Hybrid**: steps laid out visually decide the journey; AI steps handle open questions inside it |
| First channel | **Website chat**. Reusing the same bot on WhatsApp/Instagram/Facebook comes later and needs evidence first |
| Audience | **Non-technical business owners**. Advanced settings can come later |
| Engine | **Extend `apps/automation`**. No second workflow engine. `apps/chatbot` owns the widget, deployment, domains, branding and chatbot analytics |
| Editor | **Reuse the existing workflow editor** (`templates/automation/workflow_editor.html`), plus a live widget preview. A node-canvas view only if pilots show owners need it. No React |
| AI | Reuse the Akilent AI and Knowledge stack (`apps/ai`), counted against `ai_actions_day` |
| Actions | A fixed list of built-in steps; the owner switches each on per bot. An AI step can only use actions the owner explicitly allowed on it |
| Custom code nodes | None |
| Integrations shown | Only what Akilent can actually run. A step it can't run is greyed out with a reason, never shown as if it works |
| Publishing | Immutable published versions (the existing `WorkflowVersion`); drafts never affect visitors who are mid-chat |
| Payments | Akilent never takes payment for a business. Orders are recorded, and marked paid by hand |

## 3. What already exists and is reused

| Need | Already in the codebase |
|---|---|
| Workflow definition, draft vs published | `automation.Workflow` (`definition`, `draft_definition`, statuses, `priority`) |
| Immutable versions | `automation.WorkflowVersion`; each run stays pinned to the version it started on |
| Per-visitor progress that resumes when they reply | `WorkflowRun`, `WorkflowStepRun`, `WorkflowInteraction`, `InteractionAnswer` |
| Steps | `_STEP_TYPES` in `apps/automation/workflow_engine.py`: `reply_text`, `send_buttons`, `send_list`, `wait_for_reply`, `ask_question`, `start_form`, `wait`, `branch`, `add_tag`/`remove_tag`, `set_attribute`, `create_lead`, `update_lead_status`, `assign_conversation`, `notify_team`, `create_followup`, `lookup_product`, `action` (through `apps/core/actions.py`), `ai_classify`, `ai_extract`, `ai_answer`, `stop`/`exit` |
| Palette with reasons for unavailable steps | `apps/automation/builder.py` `step_catalog()` |
| Simulator, run history | `apps/automation/simulate.py`, the run views |
| Starter templates | `apps/automation/workflow_templates.py` |
| Engine limits | `_MAX_STEPS_PER_CALL` and the existing loop and deadline handling |
| Message triggers and enrolment | `conversation.message_received`, `apps/conversations/services.py` `_enroll_workflows()` |
| Chatbot identity and deployment | `chatbot.ChatbotConfig` (public key, domains, branding), `ChatSession`, `static/chat-widget/v1/chat.js` |

## 4. Architecture

```
Website visitor ──► widget (chat.js) ──► /api/chat/* (apps/chatbot/api)
                                              │
                         record_inbound_website_message (apps/conversations)
                                              │
                         Conversation(channel=website_chat) + Message
                                              │
                         _enroll_workflows → WorkflowRun on the bot's Workflow
                                              │
               steps: reply_text / send_buttons / ask_question / ai_answer / create_lead / …
                                              │
                         outbound Message saved ──► widget fetches it via poll
                                              │
                         inbox: the team sees the thread and can reply → widget gets it
```

Changes:

- **`website_chat` becomes a first-class channel.**
  - Register it in `apps/core/channels.py`: setting `WEBSITE_CHAT_ENABLED`, paths, help/docs tag,
    billing dependencies.
  - Add `record_inbound_website_message()` next to the other `record_inbound_*_message` functions.
    It publishes the same event and calls `_enroll_workflows()`.
  - Outbound: add a `website_chat` branch wherever the engine dispatches by channel (reply,
    interactive steps around `workflow_engine.py` line 1185, and the inbox send). It only **saves**
    the message; the widget fetches it by polling. There's no 24-hour window on this channel.
- **New trigger `website_chat.opened`** (the visitor opens the widget), so a bot can greet them before
  they type anything. Later messages use the existing `conversation.message_received`.
- **`ChatbotConfig.workflow`** is a new FK to `automation.Workflow`: the conversation this bot runs.
  The bot keeps its domains, branding, public key and on/off state. Matching rule: a website
  conversation from bot X only enrols bot X's workflow, never the account's other message workflows.
- **Visitor identity:**
  - From the first message, `ChatSession` gets an anonymous **visitor contact** scoped to the
    account (source `website_chat`, marked unverified) and a Conversation. The bot then remembers
    the chat, and the inbox sees every chat.
  - `identify` / `ask_question` fill in that contact. A self-reported email or phone is **never
    merged** into an existing contact. Instead the inbox suggests a possible match (open question
    §12).
- **Retired:** `apps/chatbot/services/responder.py`, the `[ACTION:slug]` marker protocol and
  owner-typed `ChatbotAction` slugs. AI runs through `ai_answer` / `ai_classify` / `ai_extract`.
  Existing `ChatbotAction` rows are migrated to steps or dropped; the feature was never released.
- **Knowledge:** the bot uses the account's Knowledge through `apps/ai`, the same as AI answers in
  the inbox. `ChatbotKnowledgeSource` becomes an optional per-bot filter, or is dropped (decide in P3).

## 5. Component map

The owner-facing categories, mapped onto engine steps. ✅ exists · 🔧 exists, needs website-chat
support · ➕ new · ⏸ later, needs evidence first.

| Category | Component | Engine step | State |
|---|---|---|---|
| Conversation | Welcome message | `website_chat.opened` trigger + `reply_text` | ➕ trigger, 🔧 reply |
| | Text / image / video message | `reply_text` (with media) | 🔧 |
| | Quick-reply buttons, multiple choice | `send_buttons`, `send_list` + `wait_for_reply` | 🔧 |
| | Free-text question saved to the customer | `ask_question` | 🔧 |
| | Email / phone input with validation | `ask_question` with a typed attribute + widget input types | 🔧 + ➕ input types |
| Logic | If/else, branching on variables | `branch` | ✅ |
| | Delay | `wait` | ✅ (short delays only on web chat; the visitor may leave) |
| | Random split for experiments | — | ⏸ |
| AI | AI answer from business knowledge | `ai_answer` | 🔧 |
| | Intent detection / routing | `ai_classify` | 🔧 |
| | Information extraction | `ai_extract` | 🔧 |
| | Product questions | `lookup_product` | 🔧 |
| Business actions | Create / update contact | visitor contact + `set_attribute` | ➕ visitor contact |
| | Create lead, qualify lead | `create_lead`, `update_lead_status` | ✅ |
| | Assign conversation, notify team | `assign_conversation`, `notify_team` | ✅ |
| | Follow-up for the team | `create_followup` | ✅ |
| | Create order | — | ⏸ (needs pilot evidence; record only, never take payment) |
| Integrations | Website chat | the channel itself | ➕ |
| | WhatsApp / Messenger / Instagram | same bot on other channels | ⏸ |
| | Webhooks / external APIs | `webhook` exists but stays hidden from Studio | ⏸ |
| Outcomes | Hand over to a person | `assign_conversation` + handoff endpoint | 🔧 |
| | Record intent / mark qualified | `set_attribute`, `update_lead_status` | ✅ |
| | Show a confirmation card | `reply_text` with a card payload | ➕ |
| | End conversation | `stop` + widget "chat ended" state | 🔧 |

The Studio palette is `step_catalog()` filtered for website chat. A step that can't run there is
greyed out with a reason, never hidden without explanation.

## 6. Widget protocol (`static/chat-widget/v1/chat.js`, `apps/chatbot/api`)

- The **API origin comes from the script's own `src`**, never from `window.location`.
- **CORS:** `OPTIONS` preflight and `Access-Control-Allow-Origin` are sent only for the matched
  allowed origin, via a small decorator in `apps/chatbot/api/auth.py`. No wildcard.
- Endpoints:
  - `init` — session + config + a published-version check
  - `open` — fires `website_chat.opened`
  - `message`
  - `poll?after=<id>` — bot and agent messages, interactive payloads, typing state
  - `identify`
  - `handoff`
- Interactive payloads: buttons, list choices, input type (text/email/phone), confirmation card,
  "chat ended".
- The session survives a reload (sessionStorage), and the widget shows earlier messages again from
  `poll?after=0`.
- Specific error states: 410 means start a new chat, 429 means "slow down", plus offline and
  retry.
- Accessibility: roles and labels, focus kept in the panel, Esc closes it, keyboard reachable.
  Design tokens include dark mode. Consent text appears on the details form.
- The snippet always uses an absolute URL and a versioned path (`/chat-widget/v1/`).

## 7. Safety and cost

- **Client IP:** `_client_ip` must stop trusting the first `X-Forwarded-For` entry, which a client can
  fake because nginx adds to it with `$proxy_add_x_forwarded_for`. Use `X-Real-IP` set by nginx from
  `$remote_addr`, or the Cloudflare header only if Cloudflare is confirmed in front.
- **Domains:** normalised and validated on save (scheme + host only). The Install page shows whether
  the widget has been seen on each domain.
- **AI cost:**
  - Every AI step reserves `ai_actions_day` like `apps/ai/tasks.py` does.
  - New per-bot daily AI cap.
  - `AI_AUTONOMY_ENABLED` and the account's AI switch are respected.
  - When a limit is hit, the step takes its declared fallback (holding reply + offer a person).
- **Abuse:** the existing init limit per IP and message limit per session stay. Add a cap on new
  sessions per bot per hour. A message for an unknown session doesn't create cache entries.
- **Engine limits:** reuse `_MAX_STEPS_PER_CALL` and the existing loop and deadline protections.
  Publishing a bot with no path to a reply, or with an AI step that has no fallback, is blocked by
  validation.
- **Access:** the management pages need the owner/admin role. The feature sits behind a `chatbot`
  plan feature in the entitlement catalogue.
- **Retention:** a scheduled task deletes anonymous sessions (no details given, no lead) after 30
  days. The privacy page and help guide say what is stored.

## 8. Studio UX (non-technical owners)

The Chatbots list shows status, domains, chats this week and leads this week. Each bot has these pages:

1. **Builder:** the existing workflow editor, with the palette filtered for website chat and the
   **live widget preview** beside it, running the *draft* through the simulator.
2. **AI & Knowledge:** tone, what the bot may and may not do, which actions AI steps may use, and
   links to the account's Knowledge. Nothing is duplicated.
3. **Appearance:** name, avatar, colour, position, welcome text.
4. **Install:** the snippet, the domain list and a check showing where the widget was last seen.
5. **Publish:** shows the draft vs live difference, then publishes a new version. Visitors already
   in a chat finish on the version they started.
6. **Analytics:** §9.

Starter templates, using the `workflow_templates.py` pattern: **Capture sales leads**,
**Answer questions, hand over when stuck**, **Help choose a product** (`lookup_product`). A new bot
starts from a template and never from an empty page.

## 9. Analytics

- **Funnel per bot and per version:** opened → sent a message → gave details → lead → qualified →
  order paid.
- **Drop-off per step**, from the existing `WorkflowStepRun` data.
- **Questions the knowledge base couldn't answer**: `ai_answer` fallbacks, linking to "add to
  Knowledge".
- **Handoffs:** how many, first-reply time, and how many became leads or orders.
- Orders and revenue come only from the existing lead/order attribution, never from estimates.

## 10. Phases

Every phase sits behind the `chatbot` release gate, ships with tests, and needs a go-ahead.

| Phase | Scope | Done when |
|---|---|---|
| **P0 Foundation fixes** | Widget origin from `script.src`; CORS + OPTIONS; domain normalisation; `_client_ip` fix; visitor contact + conversation from the first message; chat memory; retention task | A plain HTML page on another origin can chat; the thread appears in the inbox; the bot remembers turn 1 at turn 5 |
| **P1 Website chat channel** | Registry entry; `record_inbound_website_message` + `_enroll_workflows`; outbound saved + `poll`; `website_chat.opened`; inbox replies reach the widget | A `reply_text` workflow answers on the website, and an agent reply from the inbox shows up in the widget |
| **P2 Interactive steps** | Buttons, lists, `wait_for_reply`, `ask_question` and input types on website chat; palette reasons in `builder.py` | A button → question → lead journey runs in the widget and in the simulator |
| **P3 AI** | Retire the responder; `ai_answer`/`ai_classify`/`ai_extract`/`lookup_product` on website chat with fallbacks; per-step action permissions; per-bot AI cap | AI answers from Knowledge; at the cap the fallback runs; AI can't trigger actions that weren't allowed |
| **P4 Studio UX** | `ChatbotConfig.workflow`; bot pages; live preview of the draft; starter templates; install check | An owner goes from "New chatbot" to a live bot from a template without help |
| **P5 Handoff and outcomes** | `handoff` endpoint → assign + notify; confirmation card; end state; analytics funnel | Handoff shows in the inbox with the reason; the funnel matches the lead/order data |
| **P6 Release** | Channel docs (`templates/docs`), help article, `templates/public/integration` page; `messaging.md` §4; plan feature + pricing (needs approval); add `chatbot` to `RELEASED_FEATURES` | Release gate §11 passed |
| **Later** | Node-canvas view; the same bot on WhatsApp/Instagram/Facebook; random splits; webhooks in Studio; create order | Each needs pilot evidence first |

## 11. Release gate

1. A journey test passes: open → welcome → button choice → AI answer → details → lead →
   handoff → agent reply in the widget → lead in the funnel.
2. It works from a real third-party origin, with no console errors, on desktop and mobile Safari and
   Chrome.
3. AI cost limits and fallbacks are checked on staging.
4. **One pilot business runs it on its live site** and produces at least one real lead.

## 12. Open questions

1. Plan placement and pricing: which plans include Chatbot Studio, and the cap on bots/chats.
2. Visitor matching: suggest a possible existing contact in the inbox, or verify the email (a
   one-time code) before linking?
3. Orders from chat: stay out of scope until a pilot asks for it?
4. `ChatbotKnowledgeSource`: keep it as a per-bot filter, or always use all of the account's Knowledge?

## Appendix — review findings this spec addresses (2026-11-14)

Found by reading the current `apps/chatbot` (53 tests pass, but none run a visitor journey end to end):

1. `chat.js` `init()` posts to `window.location.origin`, the customer's site → 404 → fixed in P0.
2. No CORS headers; `require_POST` answers the `OPTIONS` preflight with 405 → P0.
3. `ChatSession.contact` is never set anywhere, so there is no Conversation, no memory between turns,
   and `_IDENTIFY_PROMPT` comes back forever after 3 turns, even after the visitor identifies → P0.
4. `perform_handoff()` is never called, and no action handler is registered → P5.
5. No delivery path for `website_chat`, so agent replies go nowhere → P1.
6. A faked `X-Forwarded-For` gets around the init rate limit → P0.
7. Chatbot AI calls don't count against `ai_actions_day` and ignore `AI_AUTONOMY_ENABLED` → P3.
8. Saved domains without a scheme never match the browser's Origin → P0.
9. The system prompt never describes the actions, so `[ACTION:]` markers can't happen; owners type
   free slugs; a duplicate slug gives a 500 → retired in P3.
10. No plan feature or role check; no retention; no channel docs → P0/P6.
