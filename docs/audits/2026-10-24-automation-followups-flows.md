# Audit: Automation, Follow-ups, WhatsApp Flows (2026-10-24)

This audit asks three things about each feature: is it complete, is it ready for production, and how does it fit the user journey. It reflects the state at audit time.

Plan: `C:\Users\hp\.claude\plans\automation-follow-up-whatsapp-whimsical-reddy.md`. Status column: **Fixed** (with the date), **Planned (Phase N)**, or **Open**.

## Verdict
| Area | Verdict |
|---|---|
| Automation | The core engine is sound: idempotent steps, row locks, a plan gate at enroll, and every query filtered by account. The surrounding pieces were not ready for production. |
| Follow-ups | A basic manual reminder. The automatic paths are broken, and nobody is told when a follow-up is due. |
| WhatsApp Flows | Not ready for production: republish, launch safety, channel guard and automation launch all fail. |

## Automation
| # | Finding | Evidence | Status |
|---|---|---|---|
| A1 | Starters with a plain `wait` (quiet-customer check-in, thank-you) kept AI quiet for 1–3 days after every customer message | `workflow_engine.answer_message` counted any WAITING run, and its earlier sends | Fixed 2026-10-24 (`waiting_on_customer`, sends counted only for this message) |
| A2 | Lead-qualification starter card returned a 500 (`KeyError 'quiet_days'`) | `engagement_starters.build_definition`; no install branch | Fixed 2026-10-24 (install branch + confirm modal; "didn't finish" alert on timeout/invalid) |
| A3 | Saving or PATCHing a published workflow skipped validation; API publish skipped the custom-field check | `views.workflow_save`, `api/workflow_views.py` PATCH/publish | Fixed 2026-10-24 (`automation_api.live_definition_problems`) |
| A4 | API create didn't make slugs unique, so a duplicate name returned a 500 | `api/workflow_views.py` create | Fixed 2026-10-24 (`unique_workflow_slug`) |
| A5 | A loop stranded the run "active" with no due date, blocking the contact forever | `advance_run` step cap | Fixed 2026-10-24: loops fail and alert; long chains continue on the sweeper; loops rejected at validation |
| A6 | Runs read the live definition, so edits change runs already in flight; a deleted step silently completes the run | `_steps_by_id` | Planned (Phase 1: versioned definitions) |
| A7 | Pause and archive are the same status; on resume every timer fires at once | `Workflow.Status`, `run_due` | Planned (Phase 1) |
| A8 | Late WhatsApp failures mark the run failed but don't alert or cancel interactions; webhook steps that run out of retries never touch the run | `integrations/whatsapp.py`, `tasks.py` | Planned (Phase 1) |
| A9 | No opt-out check at enroll; the 24h window is checked only at drain; no template fallback | `enroll`, `whatsapp/tasks.py:739-773` | Planned (Phase 1) |
| A10 | An `ask_question` send failure is swallowed and the run waits for an answer to a question never delivered | `workflow_engine.py` ask_question | Planned (Phase 1) |
| A11 | The `on_business_event` contact lookup is not scoped by account; there are no role checks on publish/delete | `workflow_engine.on_business_event` | Planned (Phase 1) |
| A12 | Nothing caps how many workflows answer one message | `intents.py:211` (warning only) | Planned (Phase 1 priority, Phase 7c routing contract) |
| A13 | Editor can't create `whatsapp.received`, `deal.*` or `order.*` triggers, or `ask_question`/`lookup_product`/`publish_post`/`action`/`exit` steps | `views.py:29-60` | Planned (Phase 2) |
| A14 | No run log, dry run or manual enroll UI | — | Planned (Phase 3) |
| A15 | Email starter templates send from `hello@example.com` with placeholder slugs; a missing slug sends an empty body | `workflow_templates.py` | Planned (Phase 3) |
| A16 | Starter readiness and the onboarding step require WhatsApp, which blocks Instagram-only accounts | `readiness.py:22`, `onboarding.py:673` | Planned (Phase 6) |

## Follow-ups
A `FollowUp` is a reminder for the team. It never messages the customer.

| # | Finding | Evidence | Status |
|---|---|---|---|
| F1 | Nothing fires when a follow-up is due; the `followup.due` realtime event is never published | `apps/core/realtime.py:67` | Planned (Phase 4) |
| F2 | All times are UTC ("tomorrow 9am", the custom picker, every display) | `followups.py:22-32`, `TIME_ZONE="UTC"` | Planned (Phase 4) |
| F3 | Any customer message closes every open follow-up for that contact; a staff reply never closes MISSED reminders | `services._clear_obsolete_followups` | Planned (Phase 4) |
| F4 | The Insights→follow-up policy can't be activated (`follow_up` vs `create_followup`); the executor writes an invalid `source="policy"` and `get_or_create` can raise | `insights/rules.py:117`, `insights_views.py:518,664`, `executor.py:329-339` | Planned (Phase 4) |
| F5 | No duplicate guard (action, AI apply race) | `actions.py:502`, `ai/api.py:426` | Planned (Phase 4) |
| F6 | The completion % counts customer-reply auto-closes as completed | `insights/performance.py:31` | Planned (Phase 4) |
| F7 | No assignee, snooze, edit, cancel or reopen; nothing on the contact or lead page | — | Planned (Phase 4) |
| F8 | Recovery is WhatsApp-only and sweeps inactive accounts | `recovery.py:35-45` | Planned (Phase 4) |
| F9 | Three overlapping "customer is waiting" mechanisms: recovery, Insights policy, overdue email | — | Planned (Phase 4: one pipeline) |
| F10 | The Insights act link uses `?filter=`, but the inbox reads `?view=`; stale notify_owner copy | `insights_views.py:387`, `policy_edit.html:98` | Planned (Phase 4) |

## WhatsApp Flows
| # | Finding | Evidence | Status |
|---|---|---|---|
| W1 | A failed launch left an IN_PROGRESS row that blocked the conversation and swallowed the next message | `forms.start_form` | Fixed 2026-10-24 (atomic) |
| W2 | Text typed during an open Flow was taken as a text-mode answer | `forms.record_answer` | Fixed 2026-10-24 |
| W3 | An Instagram conversation crashed with an AttributeError (500) | `forms._start_flow` | Fixed 2026-10-24 (Flow forms ask by text off WhatsApp) |
| W4 | The inbox picker offered Flows that can't be sent (unpublished / needs republish) | `views.py:718` | Fixed 2026-10-24 (`forms.can_start`) |
| W5 | Republish re-uploads to an already-published Flow, which Meta rejects (inferred from Meta's documentation, not tested against the live API) | `whatsapp/tasks.py:1657-1661` | Planned (Phase 5) |
| W6 | Meta's `validation_errors` from the upload are ignored | `providers/meta.py:373-384` | Planned (Phase 5) |
| W7 | Launching from an automation `action` step is broken (no `form` resolver) | `workflow_engine.py:1793` | Planned (Phase 5: `start_form` step) |
| W8 | No timeout or abandon sweep, and no cancel in the inbox | — | Planned (Phase 5) |
| W9 | Text, email, phone and number fields only, all required; customer-facing text is hard-coded English; Flow JSON is 3.0; no 20-character label check | `flow_json.py` | Planned (Phase 5) |
| W10 | Can't send outside 24h or in campaigns (no FLOW template button) | `template_builder.py:33` | Planned (Phase 5) |
| W11 | Flow is created on the first number's WABA, which breaks multi-WABA accounts | `tasks.py:1632` | Planned (Phase 5) |
| W12 | Answers are not mapped to email, phone or lead; no responses view | `forms._apply_mapping` | Planned (Phase 5) |
| W13 | Forms have no nav entry, plan gate or help article | `_nav.html`, `billing/features.py` | Planned (Phase 5/6: bundled with `automations`) |

## User journey (at audit time)
- **Discover:**
  - Automations and Build are in the nav (gated on `automations`).
  - Follow-ups are in the nav (CORE).
  - Forms are reachable only from an inbox header link.
  - There are no help articles for any of the three.
- **Set up:** onboarding "Turn on your first automation" needs WhatsApp. `set_followup` counts automatic MISSED rows.
- **Daily use:**
  - Follow-ups show in the pane, `/inbox/followups/` and the dashboard work queue, but nothing notifies anyone.
  - Automation failures show only as a badge and a daily email.
- **Measure:** the follow-up completion % is inflated.
- **Marketing:** claims in `docs/marketing/messaging.md` match the implementation. Forms/Flows are unclaimed.
