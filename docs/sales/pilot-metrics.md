# Pilot metrics: where each one lives

The pilot is measured on five things. This page says where each one is shown, what it
counts, and what isn't measured yet. Every number comes from recorded data; none are
estimates.

The **Pilot Command Center** (`/manage/pilot/`, operators only) has one scorecard row per
pilot business, covering the last 7 days. The business sees the same data on **Insights**
(`/insights/`), over 7, 30 or 90 days.

| Measure | Command Center (scorecard) | Business sees it on Insights | Source |
|---|---|---|---|
| **Acquisition** | New conversations customers started, plus "N from posts" | "This week" proof bar; "From conversation to sale → wrote in"; "Your customer journey → chats from posts" | `conversations.reporting.first_replies`; `publishing.results.journey` (ConversationEntryPoint) |
| **Response** | Median first reply | "This week" proof bar; Response section | `conversations.reporting.response` |
| **Conversion** | Leads created → paid orders | "Sales from conversations": leads, paid orders, lead-to-sale % | `conversations.reporting.sales` |
| **Revenue** | Paid orders credited to a conversation, per currency, plus "from posts" | "Sales from conversations" revenue; "Your customer journey → paid from posts" | `conversations.reporting.revenue`; ConversationAttribution.source_post |
| **Retention** | How many of the last 4 weeks had at least one customer message; the existing "Quiet" flag (no customer message for 3 days) | Business momentum | `conversations.api.pilot_scorecard`; `conversations.api.activity` |

Already on the Command Center before this change: time to first value (first automation
run after connecting), automations on, conversations in 7 days, AI suggestions used, and
the Starting point (unanswered before and after).

## Not measured yet

- **Retention of the business itself** (renewals, cancellations) isn't in the scorecard.
  Billing has the subscription state; it isn't brought into this view.
- **Pilot businesses without WhatsApp** don't appear: the Command Center lists businesses
  that have connected a WhatsApp number (`whatsapp.api.connected_accounts`).
- **Posts → sales** counts only explicit evidence: a post's chat link, or a reply to a
  comment on it. A customer who saw a post and wrote in some other way isn't counted, so
  the "from posts" numbers are the least that posts brought in.
- **Baseline comparison** (before Akilent vs now) exists per enrolled pilot in
  `accounts.pilot.pilot_outcome`, but isn't shown on the Command Center.
