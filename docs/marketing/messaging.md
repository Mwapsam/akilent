# Akilent messaging guide

This guide covers how Akilent talks about itself everywhere: the landing page, use-case pages, signup, onboarding, in-app prompts, help, sales demos and email. Every surface copies from here, so they all tell the same story. And every message must be **true today**.

## 1. The promise

> **Akilent turns customer conversations and social media activity into sales, from one place.**

We sell outcomes, not a feature list. A business buys Akilent for one of two reasons. Both lead into the same product.

| Angle | Lead with | For |
|---|---|---|
| **Primary: lost enquiries** | "Stop losing customers in your inbox." | Businesses whose WhatsApp/Instagram messages get missed, answered late or never followed up |
| **Secondary: social media that sells** | "Turn your social media activity into measurable results." | Businesses that post on Instagram/Facebook but can't tell which posts bring customers |

The landing headline stays channel-independent ("Turn customer conversations into opportunities you don't miss"). The sentences under it name only the channels that are on.

## 2. Four pillars

The product is one business process, not four products. Each pillar is a step in it.

| Pillar | Customer problem | Outcome we promise | What Akilent does | Boundary |
|---|---|---|---|---|
| **Talk:** capture interest | Enquiries arrive on WhatsApp and Instagram and some are missed or answered late | Fewer missed enquiries, more consistent replies | WhatsApp and Instagram DMs in one shared inbox. Auto-replies (greeting, closed hours, keywords). AI suggests replies from your business knowledge, or answers simple questions only if you allow it | Facebook Messenger is **not** in the inbox. Email does not arrive in the inbox. AI is off by default |
| **Publish:** stay visible | Posting is ad hoc and done separately on each app | A consistent social presence with less manual work | Content Studio: plan posts, write once with a version per channel, AI caption help, schedule per channel, optional review before publishing, publish to Instagram and a Facebook Page | Facebook means **Page publishing** only |
| **Automate:** follow through | Follow-ups are forgotten and the same tasks repeat | Less repetitive work, fewer forgotten follow-ups | Workflows, follow-up reminders, a nudge when a conversation goes unanswered (internal, not a customer message) | Automation steps with buttons/lists are WhatsApp-only |
| **Sell & measure:** understand what works | No one knows which post or message became a sale | A clear view from marketing activity to paid orders | Leads, orders and payment links. Campaigns by WhatsApp and email. Each post's results (clicks → chats → leads → orders → paid revenue). Business Health | Results count **explicit evidence only** (tracked link or comment reply). They are lower bounds, never estimates. Email is sending and campaigns |

## 3. The journey (the story every surface tells)

Every step below is real code. The demo (`docs/sales/demo.md`) walks through these screens in this order.

| Step | What happens | How it really works |
|---|---|---|
| **Attract** | The business plans and schedules a product post in Content Studio | `apps/publishing` composer and per-channel variants, published through the Meta Graph API |
| **Engage** | A customer taps the post's WhatsApp chat link, or comments and gets a private reply, and the chat lands in the shared inbox | The tracked link (`/go/<code>`) prefills a ref code. `apps/conversations/entry_points.py` links the conversation to the post |
| **Convert** | The team marks the customer interested (a lead), records an order and sends a payment link | `apps/crm/services.py` and `apps/commerce/services.py`. Attribution is fixed when the record is created (`apps/conversations/attribution.py`) |
| **Measure** | The post shows its clicks, chats, leads, orders and paid revenue per currency. Business Health shows the trend | `apps/publishing/results.py::results_for`, `/insights/` |

Say "where the data supports it". A sale whose customer didn't use the link or the comment reply is not credited to a post, and the page says so.

## 4. Release status (what we may sell today)

The truth rule (§6) is checked against this table. A row that isn't **Live** is never presented as available.

| Capability | Status | How copy handles it |
|---|---|---|
| WhatsApp inbox, automations, campaigns | Live | Shown while the WhatsApp channel is on |
| Email sending, templates, campaigns, API/SMTP | Live | Shown while the Email channel is on. Never "email conversations" |
| Instagram DMs, comment rules, publishing | **Live in the code; Meta App Review pending.** Until Meta approves the permissions, only accounts with a role on the Akilent Meta app can connect | The channel is **restricted**: on for accounts that can already connect, but not offered to anyone new until Meta approves. Operator Console → Settings → Channels → "Offer to new customers" stays unticked, so signup, plan cards and marketing (`public_channels()`) leave it out, and connect pages say access is limited |
| Facebook Page publishing | **Live in the code; Meta App Review pending** (pages_* scopes, same submission) | Same as Instagram: restricted until approved. Never "Facebook messages" |
| Content Studio | Live (needs Instagram or a Facebook Page to publish to) | Shown when at least one publishing channel is on |
| Leads, orders, payment links, Business Health, AI answers + knowledge | Live, depending on the plan | "Some features depend on your plan" nearby |
| Website chatbot, Support tickets, HubSpot, custom fields | **Not released** (open release gates) | Only "Coming soon" via `ComingSoonFeature` |

## 5. The pattern

Every major message follows one order:

1. **Problem:** a situation the owner recognises ("Your team gets busy and a follow-up is forgotten.")
2. **Outcome:** what they want instead ("Opportunities you don't miss.")
3. **What Akilent does:** the capability, stated plainly ("Reminds your team when a conversation needs a follow-up.")
4. **What it looks like:** the concrete behaviour or a screenshot ("A reply to a stock question, then a 'Follow up tomorrow' reminder.")

| Section | Pattern |
|---|---|
| Hero | Outcome → explanation → concrete capabilities → product screenshot |
| Problem | Problem → recognisable situation → Akilent's role |
| Channels | Channel → what it does here → what it doesn't do |
| Journey | Step → what the customer does → what the business sees |
| Features | Feature → what it does → why the owner cares → next step |
| FAQ | Question an owner asks → direct answer → the condition or limit |

Don't jump from a mechanism straight to an impressive outcome. "AI-powered omnichannel revenue automation" skips the steps. "Bring your customer conversations together and make sure the right follow-up isn't forgotten" doesn't.

## 6. The truth rule

Before a sentence goes on any surface, the answer must be yes to: **could a new customer see this working in the product today, on the plan we're showing them?**

Every claim is one of three types:

| Type | What it says | Example |
|---|---|---|
| Outcome | What the customer gets. Aspirational, never guaranteed. | "Turn customer conversations into opportunities you don't miss." |
| Capability | What Akilent does. Must be demonstrable. | "Get reminders when a conversation needs a follow-up." |
| Mechanism | How it works. Must match the code. | "WhatsApp and Instagram messages arrive in one shared inbox." |

Outcomes may be aspirational but not absolute. "Opportunities you don't miss" is fine. "Never miss a customer" is a guarantee we can't keep.

**No results without evidence.** We don't claim "more sales", "X% faster replies" or customer counts until pilot measurements show them, using the starting-point method in the Pilot Command Center. Testimonials and case studies appear only when they are real and the business gave permission.

## 7. "Does" versus "helps"

Say exactly what the product does, and let the owner draw the conclusion.

| Don't say | Say | Why |
|---|---|---|
| "Wins back missed customers automatically" | "Reminds your team to follow up when a conversation needs attention" | Missed-conversation recovery creates an internal follow-up. It doesn't message customers. |
| "AI handles your customers" | "AI can suggest replies, or answer simple questions on its own if you allow it" | AI is off by default, and suggest-only unless the owner turns on autopilot. |
| "WhatsApp, email and other conversations in one inbox" | "WhatsApp and Instagram messages in one shared inbox. Email your customers from your own address" | Only WhatsApp and Instagram DMs arrive in the inbox. Email is sending and campaigns. |
| "Manage Facebook messages" / "Facebook inbox" | "Publish and schedule posts to your Facebook Page" | Facebook is Page publishing only. There is no Messenger. |
| "See exactly which posts make you money" | "See the chats, leads, orders and paid revenue that came through each post's chat link or comment replies" | Only explicit evidence is counted, so results are lower bounds. |
| "Post everywhere" | "Publish to Instagram and your Facebook Page" | Name the real destinations. |
| "Never miss a customer" | "Opportunities you don't miss" | No absolute guarantees. |

## 8. Feature → sales message

Use these on feature cards, use-case pages, onboarding steps and in-app prompts. "Proof" is the real screen to show, as a screenshot from the demo workspace or live in a demo. "Next step" is the call to action.

| Feature | Customer problem | Headline | Proof (screen) | Next step |
|---|---|---|---|---|
| Shared inbox (WhatsApp + Instagram) | "Messages get lost between phones and people" | Every enquiry in one shared inbox | `/inbox/`: conversation list with channel badges, assignment | Connect WhatsApp / Connect Instagram |
| Auto-replies | "Customers wait when we're busy or closed" | Customers hear back even when you're closed | `/automations/`: greeting, closed-hours and keyword starters | Turn on your first automation |
| AI answers + knowledge | "We answer the same questions all day" | Answer recurring questions with your own business information | `/ai/`: knowledge list and suggested reply in the inbox | Teach AI about your business |
| Instagram comment rules | "Comments asking 'price?' go unanswered" | Turn post comments into private conversations | `/instagram/` rules | Add a comment rule |
| Content Studio | "Posting takes too long and happens irregularly" | Plan, adapt and schedule your posts in one place | `/publishing/`: composer with Instagram and Facebook versions, calendar | Plan your first post |
| Post results | "We don't know which posts bring customers" | See which posts bring chats, leads and paid orders | Post detail → Results card | Add a chat link to your next post |
| Workflows + follow-ups | "Follow-ups depend on someone remembering" | Keep every opportunity moving | `/automations/` workflow, a follow-up on a conversation | Set a follow-up |
| Leads | "Interested customers get forgotten" | Know who's ready to buy | `/sales/` leads list | Mark an interested customer |
| Orders + payment links | "Taking payment means more back-and-forth" | Record the order and send a link to pay | `/orders/`: order with payment link | Create your first order |
| Campaigns + segments (WhatsApp, email) | "Reaching past customers is manual" | Bring customers back with the right message | Campaign builder with a segment | Send your first campaign |
| Email from your domain | "Our emails look unprofessional or land in spam" | Email customers from your own address | `/email/domains/` verified domain | Add your sending domain |
| Business Health | "We can't see whether things are improving" | See what your conversations turned into | `/insights/` | Set a monthly goal |

## 9. Wording rules

- Use plain words a shop owner uses. Keep "infrastructure", "omnichannel", "platform layer" and "API-first" out of everything except the developer section.
- Use no numbers we can't prove: no uptime, delivery rate, customer counts or testimonials until we have them.
- Never present future features as current. Unreleased work appears only as "Coming soon" from `ComingSoonFeature`.
- Make the promise channel-independent and the facts channel-specific. Public copy for a channel shows only while that channel is offered to new customers (`public_channels()`: on, and not restricted). A deployment never shows a promise for a channel it doesn't offer.
- Call Facebook "Facebook Page" or "your Facebook Page" in feature copy. Never call it just "Facebook messages".
- Put "Some features depend on your plan" near plan-dependent features. Pricing bullets come from the plan's real features and limits (`billing_api.plan_card`), never from copy.
- Anything that depends on configuration (trial length, which channels are on) comes from the code, not the copy.
- Use contextual prompts in the app, never upgrade pop-ups. Suggest the next useful action from what the business has already done.

## 10. Claim register

Every claim a public page makes carries `data-claim="<id>"`. `apps/accounts/tests/test_landing_claims.py` fails in three cases:
- a page shows a claim that isn't listed here
- a product statement carries no id
- a claim's evidence no longer exists

**To add or change a claim:** add or update its row, with the code that proves it, in the same change.

Evidence is `path::text`: the file must exist and contain the text.

| id | Type | Claim (as the page states it) | Evidence |
|---|---|---|---|
| outcome | Outcome | Turn customer conversations into opportunities you don't miss | (aspirational; supported by the capabilities below) |
| promise | Outcome | Turn customer conversations and social media activity into sales, from one place | (aspirational; supported by the journey claims below) |
| lost-enquiries | Outcome | Stop losing customers in your inbox | (aspirational; supported by inbox, follow-ups and missed) |
| social-results | Outcome | Turn your social media activity into measurable results | (aspirational; supported by post-results and attribution) |
| inbox | Capability | WhatsApp conversations in one shared inbox your team can see | apps/conversations/services.py::def record_inbound_whatsapp_message |
| instagram-dms | Capability | Instagram DMs arrive in the same shared inbox | apps/conversations/services.py::def record_inbound_instagram_message |
| instagram-comments | Capability | Reply to Instagram comments automatically by keyword, privately or publicly | apps/instagram/services/triggers.py::def evaluate_triggers |
| assign | Capability | Assign a conversation to a teammate | apps/conversations/actions.py::class AssignConversationAction |
| auto-replies | Capability | Greet new customers, reply when you're closed, answer common questions by keyword | apps/automation/engagement_starters.py::Reply when you're closed |
| keywords | Mechanism | Keyword-triggered replies | apps/automation/keywords.py::def |
| business-hours | Mechanism | Replies that know when you're closed | apps/accounts/business_hours.py::def |
| follow-ups | Capability | Follow-up reminders for the team | apps/conversations/models.py::class FollowUp( |
| missed | Capability | A nudge when a conversation has gone unanswered (internal reminder, not a customer message) | apps/conversations/recovery.py::def create_missed_followups |
| workflows | Capability | Workflows that run steps for you when something happens (a message, a new lead, a paid order) | apps/automation/workflow_engine.py::def enroll( |
| customers | Capability | Customer details, tags and message history kept together | apps/contacts/models.py::class Tag( |
| interested | Capability | Track customers who are interested in buying | apps/crm/services.py::def set_lead_status |
| campaigns | Capability | Send an approved WhatsApp message or an email to a list of customers | apps/whatsapp/campaigns.py::def |
| campaign-scheduling | Capability | Schedule a WhatsApp campaign; pause, cancel or resend failed messages | apps/whatsapp/campaigns.py::def dispatch_due_campaigns |
| segments | Capability | Send a WhatsApp campaign to a segment: customers picked by rules, updated automatically | apps/contacts/segments.py::def builder_fields |
| payments | Capability | Record an order and send a link to pay online | apps/commerce/services.py::def request_payment |
| ai | Capability | AI suggests replies; answers simple questions on its own only if allowed; off by default | apps/ai/models.py::enabled = models.BooleanField(default=False) |
| ai-knowledge | Capability | Teach AI your answers: add them, import a list, or read them from your website | apps/ai/knowledge.py::def read_site |
| content-studio | Capability | Write a post once, with its own version for Instagram and for your Facebook Page | apps/publishing/composer.py::PLATFORM_LABELS = {"instagram": "Instagram", "facebook": "Facebook"} |
| content-planning | Capability | Plan posts by content pillar and campaign on a calendar | apps/publishing/models.py::class ContentPlanItem( |
| post-scheduling | Capability | Schedule each channel's version for its own time | apps/publishing/services.py::variant.scheduled_at = times.get(variant.platform) |
| post-review | Capability | Optionally require an owner or admin to approve posts before they publish | apps/publishing/models.py::requires_review = models.BooleanField(default=False) |
| ai-captions | Capability | AI drafts captions for each channel from your notes | apps/ai/drafting.py::def content_caption_prompt |
| facebook-publishing | Capability | Publish and schedule posts to your Facebook Page | apps/publishing/providers/facebook.py::class FacebookPublishProvider |
| instagram-publishing | Capability | Publish and schedule posts to Instagram | apps/publishing/providers/instagram.py::class |
| post-metrics | Capability | See each post's reach and engagement from Instagram and Facebook | apps/publishing/metrics_collector.py::def collect_variant_metrics |
| post-chat-link | Mechanism | A tracked WhatsApp chat link for each post | apps/publishing/links.py::def link_for_variant |
| post-results | Capability | See the clicks, chats, leads, orders and paid revenue each post brought, counted from its chat link and comment replies only | apps/publishing/results.py::def results_for |
| attribution | Mechanism | A lead or order is credited to the conversation, and the post, that started it, fixed when it's created, from explicit evidence only | apps/conversations/entry_points.py::Only hard evidence counts |
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

## 11. Reviewing copy

- When a feature changes, search this register for its id and re-read every sentence that carries it.
- When a channel's release status changes (§4), update §4 and §7 and the copy gated on it, in the same change.
- When a pilot business misunderstands a sentence, rewrite it using the pattern above. The confusion is evidence the copy skipped a step.
