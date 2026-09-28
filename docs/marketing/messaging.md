# Akilent messaging guide

How Akilent talks about itself on the landing page and anywhere else we describe the product. Its
job is to make every message easy to understand and **true today**.

## 1. The pattern

Every major message follows one order:

1. **Problem:** a situation the owner recognises ("Your team gets busy and a follow-up is forgotten.")
2. **Outcome:** what they want instead ("Opportunities you don't miss.")
3. **What Akilent does:** the capability, stated plainly ("Reminds your team when a conversation needs a follow-up.")
4. **What it looks like:** the concrete behaviour or example ("A reply to a stock question, then a 'Follow up tomorrow' reminder.")

Each section of the page uses a version of it:

| Section | Pattern |
|---|---|
| Hero | Outcome → explanation → concrete capabilities |
| Problem | Problem → recognisable situation → Akilent's role |
| How it works | Action → benefit → product behaviour |
| Features | Feature → what it does → why the owner cares |
| FAQ | Question an owner asks → direct answer → the condition or limit |

Don't jump from a mechanism straight to an impressive outcome. "AI-powered omnichannel revenue
automation" skips the steps; "Bring your customer conversations together and make sure the right
follow-up isn't forgotten" doesn't.

## 2. The truth rule

Before a sentence goes on the page, it must answer yes to: **could a new customer see this
working in the product today, on the plan we're showing them?**

Every claim is one of three types:

| Type | What it says | Example |
|---|---|---|
| Outcome | What the customer gets. Aspirational, never guaranteed. | "Turn customer conversations into opportunities you don't miss." |
| Capability | What Akilent does. Must be demonstrable. | "Get reminders when a conversation needs a follow-up." |
| Mechanism | How it works. Must match the code. | "WhatsApp messages arrive in one shared inbox." |

Outcomes may be aspirational but not absolute. "Opportunities you don't miss" is fine;
"Never miss a customer" is a guarantee we can't keep.

## 3. "Does" versus "helps"

Say exactly what the product does, and let the owner draw the conclusion.

| Don't say | Say | Why |
|---|---|---|
| "Wins back missed customers automatically" | "Reminds your team to follow up when a conversation needs attention" | Missed-conversation recovery creates an internal follow-up; it doesn't message customers. |
| "AI handles your customers" | "AI can suggest replies, or answer simple questions on its own if you allow it" | AI is off by default and suggest-only unless the owner turns on autopilot. |
| "WhatsApp, email and other conversations in one inbox" | "Your WhatsApp conversations in one shared inbox; email your customers from your own address" | Only WhatsApp messages arrive in the inbox today. Email is sending and campaigns. |
| "Never miss a customer" | "Opportunities you don't miss" | No absolute guarantees. |

## 4. Wording rules

- Plain words a shop owner uses. No "infrastructure", "omnichannel", "platform layer", "API-first" outside the developer section.
- No numbers we can't prove: no uptime, delivery rate, customer counts or testimonials until we have them.
- No future features presented as current. Unreleased work appears only as "Coming soon" from `ComingSoonFeature`, which the pricing cards already render.
- Channel-independent promise, channel-specific facts. The headline is about conversations and opportunities; the sentences underneath name the channels that really work.
- Plan-dependent features carry "Some features depend on your plan" near them.
- Anything that depends on configuration (trial length, WhatsApp on or off) comes from the code, not the copy.

## 5. Claim register

Every claim the landing page makes carries `data-claim="<id>"` in `templates/accounts/landing.html`.
`apps/accounts/tests/test_landing_claims.py` fails if the page shows a claim that isn't listed
here, or if a claim's evidence no longer exists. **To add or change a claim:** add or update its
row, with the code that proves it, in the same change.

Evidence is `path::text`: the file must exist and contain the text.

| id | Type | Claim (as the page states it) | Evidence |
|---|---|---|---|
| outcome | Outcome | Turn customer conversations into opportunities you don't miss | (aspirational; supported by the capabilities below) |
| inbox | Capability | WhatsApp conversations in one shared inbox your team can see | apps/conversations/services.py::def record_inbound_whatsapp_message |
| assign | Capability | Assign a conversation to a teammate | apps/conversations/actions.py::class AssignConversationAction |
| auto-replies | Capability | Greet new customers, reply when you're closed, answer common questions by keyword | apps/automation/engagement_starters.py::Reply when you're closed |
| keywords | Mechanism | Keyword-triggered replies | apps/automation/keywords.py::def |
| business-hours | Mechanism | Replies that know when you're closed | apps/accounts/business_hours.py::def |
| follow-ups | Capability | Follow-up reminders for the team | apps/conversations/models.py::class FollowUp( |
| missed | Capability | A nudge when a conversation has gone unanswered (internal reminder, not a customer message) | apps/conversations/recovery.py::def create_missed_followups |
| customers | Capability | Customer details, tags and message history kept together | apps/contacts/models.py::class Tag( |
| interested | Capability | Track customers who are interested in buying | apps/crm/services.py::def set_lead_status |
| campaigns | Capability | Send an approved WhatsApp message or an email to a list of customers | apps/whatsapp/campaigns.py::def |
| payments | Capability | Record an order and send a link to pay online | apps/commerce/services.py::def request_payment |
| ai | Capability | AI suggests replies; answers simple questions on its own only if allowed; off by default | apps/ai/models.py::enabled = models.BooleanField(default=False) |
| insights | Capability | See what your conversations turned into: replies, follow-ups and sales | apps/conversations/reporting.py::def proof |
| email-sending | Capability | Email customers from your own business address | apps/email/models.py::class EmailDomain( |
| own-number | Mechanism | Connect your own WhatsApp business number through Meta's guided setup | apps/whatsapp/embedded.py::def |
| roles | Capability | Team roles: Owner, Admin, Member | apps/accounts/models.py::MEMBER = "member", "Member" |
| data-deletion | Capability | Data deletion on request | automator/urls.py::name="data-deletion" |
| trial | Capability | N-day free trial, no card required (N from the signup plan) | apps/billing/api.py::def default_signup_trial |
| cancel | Capability | Cancel from Billing at any time | apps/billing/views.py::def cancel_subscription |
| trial-end | Capability | Nothing is deleted when a trial ends | apps/billing/tasks.py::def expire_trials |
| api | Mechanism | REST API to send email | apps/api/serializers.py::class MessageCreateSerializer |
| smtp | Mechanism | SMTP sending | apps/email/models.py::class SmtpCredential( |
| webhooks | Mechanism | Signed delivery, open and click webhooks | apps/email/webhooks.py::def build_signature_header |
| verification-codes | Mechanism | WhatsApp one-time login codes from your app | apps/whatsapp/verification_codes.py::def |

## 6. Reviewing copy

When a feature changes, search this register for its id and re-read every sentence that carries it.
When a pilot business misunderstands a sentence, rewrite it using the pattern above. The confusion
is evidence the copy skipped a step.
