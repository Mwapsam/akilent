# Five-minute demo: from a post to a paid order

**Who it's for:** a small retailer that sells through WhatsApp and Instagram and posts its products on social media.
**What it shows:** one believable customer journey, Attract → Engage → Convert → Measure, on real screens.
**What it never does:** simulate something that isn't working. A step that hasn't been checked stays out of the demo.

Wording comes from `docs/marketing/messaging.md`. If a line here disagrees with the guide, the guide wins.

## Before the demo

1. Build the demo workspace with `poetry run python manage.py seed_demo_workspace --password <pw>`. It needs DEBUG on, or `--allow-prod` on purpose.
   - Rebuild it before each demo. That resets dates so the posts read "6 days ago" and "2 days ago", and the trial stays active.
2. Sign in as `demo.owner@akilent-demo.invalid` with that password.
3. Make sure the channels you'll mention are on (Operator Console → Settings → Channels): WhatsApp, Instagram, Facebook and Email. Instagram and Facebook can stay restricted ("Offer to new customers" unticked); the demo workspace already has every feature.
4. Open these tabs in order: Content Studio library, the linen dress post, Inbox, Sales, Orders, Insights.

**What the workspace is.** "Demo Boutique (demo workspace)" holds synthetic customers, fictional phone numbers (+1 202 555-01xx) and illustration images.
- Conversations, leads, orders and post attribution were created by the product's own services. The numbers on the results card are the product's real calculation.
- Post reach/likes are illustrative numbers. Say so if asked.
- The workspace has **no** WhatsApp number, Instagram connection, Facebook Page or payment provider, so nothing in it can send, publish or charge (see "Not in this demo").

## The script

Open in one line (the primary angle):
> "Most small shops don't lose customers because of price. They lose them in the inbox. Let me show you one customer's journey from a post to a paid order."

### 1. Attract (about 1 minute): a customer sees a product post

| | |
|---|---|
| **Screen** | Content Studio → Library (`/publishing/library/`), then the "Our linen summer dress" post (`/publishing/posts/<id>/`) |
| **Show** | One post with an Instagram version and a Facebook Page version. Point to the calendar (`/publishing/calendar/`) and the planned posts for the "Summer linen" campaign (`/publishing/planning/`). Open the composer (`/publishing/new/`) to show writing once with a version per channel |
| **Say** | "You plan and write the post once. Akilent keeps a version for Instagram and one for your Facebook Page, each with its own schedule. If you want, an owner approves posts before they go out." |
| **Boundary** | Facebook is Page publishing only: no Messenger. Don't publish from the demo workspace |

- [x] Screens render with the seeded data (checked 2026-10-24 with the Django test client)
- [ ] Walked through live in a browser, on ____ (date), by ____

### 2. Engage (about 1.5 minutes): the customer messages on WhatsApp or Instagram

| | |
|---|---|
| **Screen** | Inbox (`/inbox/`). Open Thandi's conversation, then Chipo's (Instagram) |
| **Show** | Thandi tapped the post's chat link. Her first message carries the post's reference ("(ref …)"), then she asks "How much is the linen dress in size M?", and the team answered. Chipo wrote on Instagram, and her DM is in the same inbox. Peter's question has no answer yet. Open Follow-ups (`/inbox/followups/`) to show the reminder to get back to Chipo |
| **Say** | "WhatsApp and Instagram messages land in one shared inbox your whole team sees. When someone asks a price, Akilent notices they're interested. When a chat needs a follow-up, it reminds you." |
| **Boundary** | Email doesn't arrive in the inbox (it's for sending and campaigns). AI suggests replies only when it's switched on, and answers on its own only if you allow it |

- [x] Screens render with the seeded data (checked 2026-10-24)
- [ ] Walked through live in a browser, on ____ (date), by ____

### 3. Convert (about 1.5 minutes): the enquiry becomes a lead and a paid order

| | |
|---|---|
| **Screen** | Sales (`/sales/`), then Orders (`/orders/`) and Thandi's paid order |
| **Show** | The leads were opened from the conversations, with no typing into a CRM. Joseph's order is recorded but not paid yet (Pending); Thandi's and Grace's are paid. When creating an order you pick the conversation it came from, and that link is what the post results use in step 4 |
| **Say** | "The moment someone is ready to buy, they're on your sales list. You record the order and send a link to pay, and Akilent remembers which chat it came from." |
| **Boundary** | Payment links need a payment provider set up. The demo workspace has none, so show the existing orders rather than creating a payment link live |

- [x] Screens render with the seeded data (checked 2026-10-24)
- [ ] Walked through live in a browser, on ____ (date), by ____

### 4. Measure (about 1 minute): which post brought the sale

| | |
|---|---|
| **Screen** | Back to the linen dress post → **Results** card. Then Insights (`/insights/`) |
| **Show** | Per channel: link clicks → chats → leads → orders → paid revenue (Instagram: a $45 paid order; Facebook: an order not paid yet, so no revenue). Business Health shows what conversations turned into over time |
| **Say** | "Now you can see which post brought the chat, the lead and the paid order. It counts only customers who came through the post's chat link or a comment reply, so the numbers are never guessed." |
| **Boundary** | Say "where the data supports it". A buyer who didn't use the link isn't credited to the post. The figures are lower bounds, not estimates |

- [x] Screens render with the seeded data (checked 2026-10-24)
- [ ] Walked through live in a browser, on ____ (date), by ____

Close with the promise and a next step:
> "That's the whole loop: post, conversation, lead, paid order, and the proof of which post did it. Shall we connect your WhatsApp number and set up your first post?"

## If they ask (optional, 30 seconds each)

| Question | Screen | Checked |
|---|---|---|
| "Can it answer common questions?" | Settings → AI knowledge (`/settings/ai/knowledge/`) | [x] renders 2026-10-24 · [ ] live |
| "Can it reply to Instagram comments?" | Instagram comment rules (`/instagram/rules/`) | [x] renders 2026-10-24 · [ ] live |
| "Can it follow up for me?" | Automations (`/automations/`), Build (`/build/`) | [x] renders 2026-10-24 · [ ] live |
| "Can I email my customers?" | Email → Domains (`/email/domains/`) | [x] renders 2026-10-24 · [ ] live |
| "How much does it cost?" | Pricing on the landing page. Quote only the plan cards; the matrix in `docs/billing/pricing-review.md` isn't approved | – |

## Not in this demo (and why)

| Left out | Why | To show it, use |
|---|---|---|
| Publishing a post live | The demo workspace has no Instagram or Facebook Page connection. Instagram/Facebook permissions are in Meta App Review, so only accounts with a role on the Akilent Meta app can connect | A test workspace connected to a test Page and IG account with an app role |
| Sending a reply live | No WhatsApp number in the demo workspace | A workspace with your own connected test number |
| Creating a payment link and paying | No payment provider in the demo workspace | A test workspace with the provider in test mode |
| Fresh post metrics | Collection is off for demo posts (they don't exist on Meta) | A real published post in a connected workspace |
| Website chatbot, Support, HubSpot, custom fields | Not released | Mention only as "coming soon" if asked |

## Keeping this script honest

- **Before a demo:** if a "live" box is unticked for a step, do a walkthrough first. If something doesn't work, drop it from the script and say so in this file.
- **After a product change** to any screen above, re-run `seed_demo_workspace`, walk the step again, and update its date.
- **Collect objections:** write down every question a prospect asked that the demo didn't answer. Those are candidates for the FAQ and for pilot interviews.
