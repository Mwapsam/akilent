# Meta App Review — submission pack

For the Akilent Meta app (`1080201821670178`). Paste the text below into the App Review
form. Request **exactly** the twelve permissions in the two tables; anything else gets the
whole submission rejected for "requesting permissions the app doesn't use". Akilent has two
separate login flows, and each table matches the scopes that flow asks for:

- Instagram Business Login → `SCOPES` in `apps/instagram/oauth.py`
- Facebook Login (Page publishing) → `_FB_SCOPES` in `apps/publishing/views.py`

Keep them in step: a scope a connect flow asks for but the review doesn't cover (or the
reverse) is also grounds for rejection. `apps/instagram/tests/test_app_review_pack.py`
checks both tables. WhatsApp (`whatsapp_business_messaging`, `whatsapp_business_management`)
already has Advanced Access and is not part of this submission.

## Instagram permissions

| Permission | Why Akilent needs it |
|---|---|
| `instagram_business_basic` | Identify the connected business account (id, username, name) and show it in Settings. |
| `instagram_business_manage_messages` | Receive the business's Instagram DMs in the Akilent inbox and send the business's replies. |
| `instagram_business_manage_comments` | Receive comments on the business's posts, send a private reply the business set up, hide/delete abusive comments the business chose to moderate, and post the business's own first comment on a post it published. |
| `instagram_business_content_publish` | Publish the posts a business creates and schedules in Akilent's Content Studio to its own Instagram account. |
| `instagram_business_manage_insights` | Show the business how each post it published from Content Studio performed (reach, views, saves, shares). |

## Facebook Page permissions

| Permission | Why Akilent needs it |
|---|---|
| `pages_show_list` | List the Pages the person manages so they can choose which Page to connect. |
| `business_management` | Find Pages owned by the person's business portfolio (`/me/businesses` → `owned_pages`) when they don't hold a direct Page role. |
| `pages_manage_posts` | Publish the posts a business creates and schedules in Content Studio to its connected Page. |
| `pages_manage_engagement` | Post the business's own first comment under a post Akilent just published for it. |
| `pages_read_user_content` | Read the comment count on a post Akilent published for the business (never the comments' text). Meta requires it with `pages_manage_engagement`. |
| `pages_read_engagement` | Read the published post's id and share count to show results next to the post. |
| `read_insights` | Read views, reach and reactions for posts Akilent published to the Page. |

**Before submitting**
1. Business Verification is complete for the business portfolio that owns the app (passes as of 2026-10-22).
2. App Dashboard → Use cases → **Manage everything on your Page** → Customize: add
   `pages_manage_engagement` and `pages_read_user_content`. Until a permission is added to the
   use case, the Facebook dialog shows "Invalid Scopes" to developers and silently drops it
   for everyone else.
3. The pending submission contains exactly the twelve permissions above. Remove anything
   else: `instagram_content_publish`, `instagram_basic`, `instagram_manage_messages`,
   `instagram_manage_comments`, `email`, `manage_app_solution`. (`public_profile` is granted
   by default with Facebook Login and needs no separate request.)
4. App settings → Basic: privacy policy `https://akilent.com/privacy/`, terms
   `https://akilent.com/terms/`, data deletion `https://akilent.com/data-deletion/`,
   a **verified** contact email on an Akilent address, and a Support URL.
5. Instagram → API setup with Instagram Login → Business login settings:
   - Redirect URI: `https://akilent.com/instagram/accounts/connect/oauth/callback/`
   - Deauthorize callback: `https://akilent.com/instagram/deauthorize/`
   - Data deletion request: `https://akilent.com/instagram/data-deletion/`
6. Facebook Login → Settings → Valid OAuth Redirect URIs includes
   `https://akilent.com/publishing/fb/callback/`.
7. Every permission has a successful API call from the live app in the last 30 days
   (the "API test call" step). Run each flow in the test instructions once on production
   with tester accounts, then wait up to 24 hours for Meta to register the calls.
8. Create a reviewer login on akilent.com: a workspace with the `instagram` feature on (use
   an operator override, it isn't on any plan yet), Owner role, an Instagram professional
   test account, and a Facebook Page the reviewer's test user manages. Add a second
   Instagram account the reviewer can message and comment from. Until approval, both
   Instagram accounts must have a role on the app (Instagram Tester), or replies fail with
   `code=100 "The requested user cannot be found"`.
9. Publish one Instagram post and one Facebook post from Content Studio a day before
   recording, so their Performance cards already show numbers.

---

## App description (for "Tell us how your app works")

Akilent is a customer-messaging inbox for small businesses. A business connects its own
Instagram professional account with Instagram Business Login. Its customers' DMs then
appear in Akilent next to the business's WhatsApp conversations, so one team can answer
them in one place. Businesses can also set up an automatic private reply when someone
comments a keyword (for example "price") on one of their posts, and hide abusive comments.

In Akilent's Content Studio a business writes and schedules its own posts and publishes
them to its connected Instagram account and Facebook Page (connected with Facebook Login).
After a post is published, Content Studio shows how it performed. Akilent publishes only
posts the business created and approved, reads insights only for posts it published for
the business, and never messages anyone who hasn't first messaged or commented on the
business.

## Per-permission usage text

### Instagram

**instagram_business_basic**
After the business logs in with Instagram, Akilent reads the account's id, username and
name to show which account is connected in Settings → Instagram and to match incoming
webhook events to the right business.

**instagram_business_manage_messages**
When a customer sends the business a DM, Akilent receives it by webhook and shows it in the
business's inbox. A team member reads it and types a reply in Akilent, which is sent with
the Send API from the business's account. Replies are only sent inside the 24-hour window
after the customer's last message: Akilent checks this itself before every send and
refuses a reply once the window has closed (it does not use the HUMAN_AGENT tag). Messages the business sends from the Instagram app also
appear in the inbox (message echoes), so the conversation stays complete.

**instagram_business_manage_comments**
Akilent receives comment webhooks for the business's own posts. If the business created a
comment rule (for example: comment contains "price"), Akilent sends that commenter one
private reply with the business's prepared message, which opens a DM conversation in the
inbox. Businesses can also choose to hide or delete comments that match their moderation
rules (spam, abuse). Each comment gets at most one automatic private reply. When a business
publishes a post from Content Studio with a "First comment" filled in, Akilent posts that
comment from the business's account under its own new post.

**instagram_business_content_publish**
In Content Studio a team member writes a post (caption plus image or video), picks the
connected Instagram account as a channel, and publishes it now or schedules it. At that
time Akilent creates a media container on the business's account and publishes it with
`/media_publish`, then shows the published post's status and link in Akilent. Nothing is
published that the business didn't create; posts that need approval are held until
someone approves them.

**instagram_business_manage_insights**
After Akilent publishes a post for the business, Content Studio shows how that post is
doing: reach, views, saves, shares and interactions, read from `/{media-id}/insights`
for that post only. Akilent reads insights only for media it published on the business's
behalf. It refreshes them a few times in the first weeks after publishing (about 1 hour,
6 hours, 1, 3, 7 and 28 days), and the business can refresh by hand. The numbers are shown
only to the business's own team, next to the post.

### Facebook Pages

**pages_show_list**
When a business owner clicks **Connect Facebook Page** (Settings → Integrations, or
Content Studio → Channels), Akilent calls `/me/accounts` to list the Pages they manage.
If there is one Page it is connected; if there are several, the owner picks one from a
list. Akilent stores only the chosen Page's id, name and Page access token.

**business_management**
Many small businesses manage their Page through a business portfolio rather than a direct
Page role, so `/me/accounts` returns nothing for them. In that case Akilent reads
`/me/businesses` and each business's `owned_pages` to find the Pages they can publish to,
and shows them in the same Page picker. Akilent does not change any business portfolio
settings, assets or people.

**pages_manage_posts**
In Content Studio a team member writes a post (text, image or video), picks the connected
Facebook Page as a channel, and publishes it now or schedules it. At that time Akilent
publishes it to the Page with `/{page-id}/feed`, `/photos` or `/videos`, then shows the
post's status and link in Akilent. Nothing is published that the business didn't create;
posts that need approval are held until someone approves them.

**pages_manage_engagement**
In Content Studio's composer, the **More options** card lets a business write a "First
comment" for its Facebook post, for example a link, a price list or extra hashtags. Right
after Akilent publishes the post to the business's connected Page, it posts that one comment
as the Page under that same post with `POST /{post-id}/comments`. The business writes the
comment itself and sees it on its Page straight away. Akilent posts at most one comment per
post, only on posts it just published for the business, and never comments on other
people's posts, replies to visitors, or likes, hides or deletes anything on the Page.
(Screencast: video 9.)

**pages_read_user_content**
After a post is published, Content Studio's **Performance** card shows how many comments
people left on it, next to its views, reactions and shares. Akilent reads only the total,
with `GET /{post-id}?fields=comments.summary(true).limit(0)`, and only for posts it published
for the business. It never reads, stores or shows the comments' text or who wrote them. The
count refreshes a few times in the first weeks after publishing and when the business clicks
**Refresh numbers**, and is shown only to the business's own team. (Screencast: video 10.)

**pages_read_engagement**
After publishing, Akilent reads the post's id and share count so the post's **Performance**
card in Content Studio shows how many people shared it. Akilent reads only posts it
published for the business.

**read_insights**
For the same posts, Akilent reads `/{post-id}/insights` (views, unique viewers and
reactions by type) and shows them on the post's Performance card. Like Instagram, the
numbers refresh a few times in the first weeks and on demand, and are only shown to the
business's own team.

## Test instructions for the reviewer

1. Go to `https://akilent.com/` and sign in with the reviewer credentials provided.
2. Open **Settings → Channels → Instagram** and click **Connect Instagram**. Log in with the
   test Instagram professional account and approve the permissions. The account appears
   as connected.
3. From the second Instagram account, send a DM to the connected account. Open **Inbox**: the
   message appears within a few seconds.
4. Type a reply in the inbox and press Send. It arrives in the Instagram conversation.
5. Under **Settings → Channels → Instagram → Manage rules** (Comment rules), create a rule "keyword: price → reply:
   Thanks! We've sent you the details in a DM." From the second account, comment "price?"
   on any post of the connected account. The commenter receives the private reply, and
   the conversation appears in the inbox.
6. Open **Settings → Integrations** and click **Connect Facebook Page**. Log in with the
   test Facebook user and approve the permissions. If the user manages several Pages,
   choose the test Page. The Page name appears as connected.
7. Open **Content Studio → New post**. Write a caption, add an image, and choose both the
   Instagram account and the Facebook Page as channels. In **More options**, fill in a
   "First comment" for each. Publish. The post appears on the Instagram profile and the
   Facebook Page with the first comment under it, and Content Studio shows it as published.
8. Open an already published post in Content Studio. Under **Performance**, click **Refresh
   numbers**. Reach, views, saves and shares (Instagram) and views, reactions, comments and
   shares (Facebook) appear. Meta can take a few minutes to report numbers for a new post.

## Screencasts (one video per permission)

Meta wants a separate video for each permission, uploaded in that permission's section of
the form. Twelve videos, each 30–90 seconds.

**Rules for every video**
- English UI, normal speed, 1080p or better, no music. Add an on-screen caption naming the
  permission in the first seconds, e.g. "pages_manage_engagement: the Page's own first
  comment".
- Start signed in to Akilent (except the two "connect" videos, which start from Settings).
  Don't show passwords or the access-code field.
- Show both sides: Akilent and the result on Instagram or Facebook (a second browser window,
  or a phone mirrored next to Akilent).
- Each "connect" video must show Meta's permission dialog with the permission listed.
  Pause on it for two seconds.
- End on the result the permission made possible, not on Akilent's success toast.
- Use the same test accounts as the reviewer instructions: Instagram account A (connected),
  Instagram account B (the customer), the test Facebook user and Page.

Before recording: connect both accounts once, and publish one Instagram and one Facebook post
from Content Studio a day early, so videos 5 and 10–12 have numbers to show.

### Instagram

**1. `instagram_business_basic`** (`01-ig-basic.mp4`, about 45 s)
1. Settings → Channels → Instagram → **Connect Instagram**.
2. Instagram login with account A → pause on the permission screen → **Allow**.
3. Back in Akilent: the account card shows account A's username and name.
4. Caption: "We read the account's id, username and name to show which account is connected."

**2. `instagram_business_manage_messages`** (`02-ig-messages.mp4`, about 60 s)
1. On account B (phone), send account A: "Hi, is the blue bag available?"
2. Akilent **Inbox**: the message arrives in a few seconds. Open the conversation.
3. Type "Yes! It's K450. Want us to hold one?" → **Send**.
4. Show the reply arriving on account B's phone.
5. Caption: "Customer DMs arrive in the inbox; the business replies from Akilent, within 24 hours."

**3. `instagram_business_manage_comments`** (`03-ig-comments.mp4`, about 90 s)
1. Settings → Channels → Instagram → **Manage rules**: create "keyword: price → reply:
   Thanks! We've sent you the details in a DM."
2. On account B, comment "price?" on a post of account A.
3. Show the private reply arriving in account B's DMs, and the conversation in the inbox.
4. Show a moderation rule hiding a spam comment, and the comment hidden on the post.
5. Content Studio → New post → Instagram channel → More options → "First comment" →
   Publish → show the first comment under the new post on account A's profile.
6. Caption: "One private reply per comment, moderation the business sets up, and the
   business's own first comment."

**4. `instagram_business_content_publish`** (`04-ig-publish.mp4`, about 60 s)
1. Content Studio → **New post**: caption plus image → choose the Instagram account.
2. **Publish**. Content Studio shows it as published, with a link.
3. Open account A's profile: the post is there.
4. Caption: "The business publishes its own post from Content Studio."

**5. `instagram_business_manage_insights`** (`05-ig-insights.mp4`, about 45 s)
1. Content Studio → open the Instagram post published the day before.
2. **Performance** → **Refresh numbers**: reach, views, saves and shares appear.
3. Caption: "The business sees how its own post performed. Only posts Akilent published."

### Facebook Page

**6. `pages_show_list`** (`06-fb-pages-show-list.mp4`, about 60 s)
1. Settings → Integrations → **Connect Facebook Page**.
2. Facebook login with the test user → pause on the permission screen → **Continue**.
3. The Page picker lists the user's Pages → choose the test Page → it shows as connected.
4. Caption: "We list the Pages the person manages so they can choose which one to connect."

**7. `business_management`** (`07-fb-business-management.mp4`, about 60 s)
1. Use a Facebook user who reaches the Page through a business portfolio, not a direct
   Page role. Settings → Integrations → **Connect Facebook Page** → permission screen →
   **Continue**.
2. The Page picker shows the portfolio's Pages → choose one → connected.
3. Caption: "For Pages managed through a business portfolio, we read the portfolio's own
   Pages to offer them. We never change portfolio settings, assets or people."

**8. `pages_manage_posts`** (`08-fb-manage-posts.mp4`, about 60 s)
1. Content Studio → **New post**: text plus image → choose the Facebook Page.
2. **Publish**. Content Studio shows it as published.
3. Open the Facebook Page: the post is at the top.
4. Caption: "The business publishes its own post to its Page."

**9. `pages_manage_engagement`** (`09-fb-manage-engagement.mp4`, about 60 s)
1. Content Studio → **New post** → choose the Facebook Page → **More options** → fill in
   "First comment" (e.g. "Order here: akilent.com/shop").
2. **Publish**.
3. Open the post on the Facebook Page: the first comment is under it, posted as the Page.
4. Caption: "One first comment, written by the business, under the post Akilent just
   published. We never comment elsewhere."

**10. `pages_read_user_content`** (`10-fb-read-user-content.mp4`, about 45 s)
1. On the Facebook Page, show the post published the day before and its comments.
2. Content Studio → open that post → **Performance** → **Refresh numbers**.
3. The comment count matches the Page. Point out that no comment text is shown.
4. Caption: "We read only the comment total on the business's own post, never the comments."

**11. `pages_read_engagement`** (`11-fb-read-engagement.mp4`, about 45 s)
1. Content Studio → the same Facebook post → **Performance** → **Refresh numbers**.
2. Highlight the share count and the link to the post.
3. Caption: "We read the post's id and share count for posts Akilent published."

**12. `read_insights`** (`12-fb-read-insights.mp4`, about 45 s)
1. Content Studio → the same Facebook post → **Performance** → **Refresh numbers**.
2. Highlight views, unique viewers and reactions by type.
3. Caption: "The business sees views and reactions for its own post, shown only to its team."

Videos 10–12 can be recorded in one sitting from the same post. Export them as three files
with their own captions, since each permission's section needs its own upload.

## After approval

- Add the `instagram` feature to the plans that should include it (Console → Plans).
- Every Facebook Page connected before 2026-10-19 must reconnect once to pick up
  `read_insights` and `pages_manage_engagement`; every Page connected before 2026-10-23
  must reconnect once more for `pages_read_user_content`.
- Watch the logs for `Instagram webhook: signature mismatch` and
  `refresh_instagram_tokens` failures for the first week.
