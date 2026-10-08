# Instagram App Review — submission pack

For the Akilent Meta app (`1080201821670178`). Paste the text below into the App Review
form. Request **only** these three permissions; anything else gets the whole submission
rejected for "requesting permissions the app doesn't use".

| Permission | Why Akilent needs it |
|---|---|
| `instagram_business_basic` | Identify the connected business account (id, username, name) and show it in Settings. |
| `instagram_business_manage_messages` | Receive the business's Instagram DMs in the Akilent inbox and send the business's replies. |
| `instagram_business_manage_comments` | Receive comments on the business's posts, send a private reply the business set up, and hide/delete abusive comments the business chose to moderate. |

**Before submitting**
1. Business Verification is complete for the business portfolio that owns the app.
2. Remove from the pending submission: `instagram_business_content_publish`,
   `instagram_content_publish`, `instagram_basic`, `instagram_manage_messages`,
   `instagram_manage_comments`, `pages_show_list`, `pages_read_engagement`,
   `business_management`.
3. App settings → Basic: privacy policy `https://akilent.com/privacy/`, terms
   `https://akilent.com/terms/`, data deletion `https://akilent.com/data-deletion/`,
   a verified contact email on an Akilent address.
4. Instagram → API setup with Instagram Login → Business login settings:
   - Redirect URI: `https://akilent.com/instagram/accounts/connect/oauth/callback/`
   - Deauthorize callback: `https://akilent.com/instagram/deauthorize/`
   - Data deletion request: `https://akilent.com/instagram/data-deletion/`
5. Create a reviewer login on akilent.com (a workspace with the `instagram` feature on
   its plan) and an Instagram professional test account the reviewer can message from.
   Add the test account as an Instagram Tester on the app.

---

## App description (for "Tell us how your app works")

Akilent is a customer-messaging inbox for small businesses. A business connects its own
Instagram professional account with Instagram Business Login. Its customers' DMs then
appear in Akilent next to the business's WhatsApp conversations, so one team can answer
them in one place. Businesses can also set up an automatic private reply when someone
comments a keyword (for example "price") on one of their posts, and hide abusive comments.
Akilent never posts content and never messages anyone who hasn't first messaged or
commented on the business.

## Per-permission usage text

**instagram_business_basic**
After the business logs in with Instagram, Akilent reads the account's id, username and
name to show which account is connected in Settings → Instagram and to match incoming
webhook events to the right business.

**instagram_business_manage_messages**
When a customer sends the business a DM, Akilent receives it by webhook and shows it in the
business's inbox. A team member reads it and types a reply in Akilent, which is sent with
the Send API from the business's account. Replies are only sent inside the 24-hour window
after the customer's last message. Messages the business sends from the Instagram app also
appear in the inbox (message echoes), so the conversation stays complete.

**instagram_business_manage_comments**
Akilent receives comment webhooks for the business's own posts. If the business created a
comment rule (for example: comment contains "price"), Akilent sends that commenter one
private reply with the business's prepared message, which opens a DM conversation in the
inbox. Businesses can also choose to hide or delete comments that match their moderation
rules (spam, abuse). Each comment gets at most one automatic private reply.

## Test instructions for the reviewer

1. Go to `https://akilent.com/` and sign in with the reviewer credentials provided.
2. Open **Settings → Channels → Instagram** and click **Connect Instagram**. Log in with the
   test Instagram professional account and approve the permissions. The account appears
   as connected.
3. From a second Instagram account, send a DM to the connected account. Open **Inbox**: the
   message appears within a few seconds.
4. Type a reply in the inbox and press Send. It arrives in the Instagram conversation.
5. Under **Settings → Channels → Instagram → Manage rules** (Comment rules), create a rule "keyword: price → reply:
   Thanks! We've sent you the details in a DM." From the second account, comment "price?"
   on any post of the connected account. The commenter receives the private reply, and
   the conversation appears in the inbox.

## Screencast script (one video, about 2–3 minutes)

Record at normal speed with captions; show the Instagram app/web next to Akilent.

1. **0:00 Login.** Sign in to Akilent. Caption: "A business connects its own Instagram account."
2. **0:15 Connect.** Settings → Instagram → Connect Instagram → Instagram login dialog → show
   the permission screen listing the three permissions → approve → connected account card.
   Caption: "instagram_business_basic: we show which account is connected."
3. **0:45 Receive.** From a second phone/account send "Hi, is the blue bag available?". Show it
   arriving in the Akilent inbox. Caption: "instagram_business_manage_messages: customer DMs
   arrive in the inbox."
4. **1:10 Reply.** Type "Yes! It's K450 — want us to hold one?" and send. Show it arriving on
   the customer's phone. Caption: "The business replies from Akilent."
5. **1:35 Comment rule.** Show the comment rule. On the customer account, comment "price?"
   on a post. Show the private reply DM arriving on the customer's phone and the
   conversation in the inbox. Caption: "instagram_business_manage_comments: one private
   reply to a comment, set up by the business."
6. **2:10 Moderation.** Show a rule hiding a spam comment, and the comment hidden on the post.
7. **2:25 Disconnect.** Settings → Instagram → Disconnect. Caption: "The business can
   disconnect at any time; Meta's deauthorize and data-deletion callbacks are supported."

## After approval

- Add the `instagram` feature to the plans that should include it (Console → Plans).
- Watch the logs for `Instagram webhook: signature mismatch` and
  `refresh_instagram_tokens` failures for the first week.
