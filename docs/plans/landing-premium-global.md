# Landing page: premium, global SaaS feel (plan of 2026-11-07)

## Context
The 2026-11-07 walkthrough of akilent.com found:
- **Slow:** LCP 5.7 s, and the page was never cached (`cf-cache-status: DYNAMIC`).
- **Motion:** scroll-ins were slow enough to leave sections faded.
- **Inconsistent:** the top-bar button changed colour as you scrolled, and backgrounds alternated without a rule.
- **Long:** the page was 11,432 px.
- **Local:** the main story used Kwacha amounts and "Paid by MTN MoMo".
- **No proof:** no customer proof and no demo path.

The positioning is decided: **"The customer conversation platform for teams that sell through chat"**, sold globally.

Approval authorised **Phases 0 and 1**. Phases 2 (global content and trust), 3 (pricing) and 4 (finish) each need their own go-ahead. No commits.

Decisions taken on approval:
- **Stylesheet:** the landing page gets its own Tailwind entry on the app's shared tokens, instead of loading the dashboard's `app.css`.
- **Demo form:** the real "Book a demo" form moves forward into Phase 1.
- **Analytics:** one GA4 event, `cta_click {cta, plan, location}`.
- **Cloudflare:** you create the cache rule; `deploy.yml` purges "/".

## Where things live
| Part | Path |
|---|---|
| View (cache headers), demo view | `apps/accounts/views.py` (`landing`, `demo`) |
| Page and sections | `templates/accounts/landing.html`, `templates/accounts/landing/_*.html` |
| Illustrations | `templates/accounts/landing/art/` (`_hero_scene`, `_flow`, `_sales` are new) |
| Demo page | `templates/public/demo.html`, model `DemoRequest`, form `DemoRequestForm` |
| Shared tokens | `assets/tokens.css`, imported by `assets/app.css` and `assets/landing.css` |
| Landing styles | `assets/landing.css`, built to `static/css/landing.css` |
| Landing script | `static/js/landing.js` (deferred) |
| Copy rules | `docs/marketing/messaging.md` §1 (positioning) and §10 (claim `positioning`) |
| Checks | `apps/accounts/tests/test_landing_premium.py`, `tools/qa/landing_qa.mjs`, `lighthouserc.json` and the `lighthouse` job in `.github/workflows/ci.yml` |

Build both stylesheets with Tailwind 4.3.3:
```
tailwindcss -i assets/app.css -o static/css/app.css --minify
tailwindcss -i assets/landing.css -o static/css/landing.css --minify
```
CI rebuilds both and compares them with what is committed.

## Phase 0: measure and speed (built)

### Baseline, before any change (Lighthouse 12.6.1 on live akilent.com, logged out, 2026-11-07)
| | Performance | Accessibility | Best practices | SEO | LCP | FCP | TTFB | CLS | Weight | Requests |
|---|---|---|---|---|---|---|---|---|---|---|
| Mobile | 76 | 100 | 93 | 92 | 3.36 s | 2.86 s | 1.43 s | 0 | 343 KB | 14 |
| Desktop | 87 | 100 | 93 | 92 | 1.40 s | 1.40 s | 0.97 s | 0.004 | 343 KB | 14 |

- The LCP element was the hero's lead paragraph, held back by 214 KB of render-blocking `app.css`.
- WebPageTest from three regions was **not run**: it needs an API key or a manual run, so that part is still open.

### What changed
- **Cacheable logged-out page:**
  - An anonymous visit with no pending message gets `Cache-Control: public, max-age=0, s-maxage=600`.
  - A signed-in visit, or one showing a message, gets `no-store, private`.
- **Purge on deploy:** `deploy.yml` purges `https://akilent.com/` after each release, using `CLOUDFLARE_ZONE_ID` and `CLOUDFLARE_API_TOKEN`. Without them it only warns.
- **Own stylesheet:**
  - The landing page loads one 55 KB file (11 KB gzipped) instead of 214 KB plus 57 KB.
  - Its tokens are the app's, from `assets/tokens.css`, and the app's build is unchanged rule for rule.
- **Headline first:**
  - Nothing in the hero waits for a script or a reveal.
  - The hero picture draws its final frame on first paint.
  - A metric-matched Arial fallback keeps line breaks steady while Inter loads.
- **Scripts:** the page script is deferred (`static/js/landing.js`), and GA still loads on idle.
- **Budgets:**
  - **pytest:** HTML under 60 KB gzipped, script under 50 KB, CSS under 16 KB gzipped, no blocking script in `<head>`.
  - **Lighthouse CI:** median of 3 runs, all categories ≥ 90, LCP < 2.5 s, CLS < 0.05.

### After Phases 0 and 1 (Lighthouse 12.6.1, local build behind a gzip nginx, logged out)
| | Performance | Accessibility | Best practices | LCP | FCP | CLS | Weight | Requests |
|---|---|---|---|---|---|---|---|---|
| Mobile (3 runs) | 92–96 | 100 | 100 | 1.54–1.69 s | 1.12–1.27 s | 0 | 72 KB | 6 |
| Desktop | 99 | 100 | 100 | 0.55 s | 0.32 s | 0 | 72 KB | 6 |

- **Locally, not live:** these are local numbers, so TTFB isn't comparable. On akilent.com, the Cloudflare cache should bring the 1.0–1.4 s TTFB down further once the rule is on.
- **SEO:** it reads 61 locally because the local site sends `noindex`. With indexing on, as in CI and production, the only SEO miss is robots.txt's deliberate `Content-Signal` line, which also fails on live (SEO 92).
- **Two measures behind the mobile numbers:**
  - The page's own small stylesheet.
  - `content-visibility: auto` on the sections below the hero. Without it, the first layout of the whole page took about 400 ms on a throttled phone.
- **Still to run after your deploy:** live before/after numbers from WebPageTest in three regions, which need a WebPageTest key or a manual run.

### You need to do (Cloudflare and GitHub)
1. **Cloudflare cache rule.** Go to Caching → Cache Rules → Create, and name it "Landing page, logged out".
   - **When:** `(http.host eq "akilent.com" and http.request.uri.path eq "/" and not http.cookie contains "sessionid" and not http.cookie contains "messages")`
   - **Then:** Eligible for cache. Edge TTL: use cache-control header if present. Browser TTL: respect origin.
2. **GitHub secrets:**
   - `CLOUDFLARE_ZONE_ID`, and `CLOUDFLARE_API_TOKEN` with the Zone → Cache Purge permission.
   - `SALES_NOTIFY_EMAIL`, where demo requests are emailed. Without it they go to the site's support address.
3. **After deploy:** load akilent.com twice in a private window. The second response should show `cf-cache-status: HIT`.

## Phase 1: first impression (built)
- **Hero:**
  - The positioning line is the headline, followed by a two-sentence lead that names only live channels.
  - "Start free trial" is the one amber button, next to "Book a demo".
  - The trial line follows, then a proof strip: "Works with …", live channels only.
  - A wide inbox scene sits underneath: list, thread, AI draft, details panel. Its 5-step loop holds the final frame, replays only while on screen, and stays still under reduced motion and without JavaScript.
- **One visual system:**
  - A dark hero, light sections split by hairlines, and a dark close (developers, final call to action, footer).
  - The top bar is frosted at every scroll position, with one light button style.
  - Radius and motion come from the app's tokens.
  - Reveals take 200 ms with an 8 px rise, on blocks only, never whole sections.
- **Scroll-spy:** a link is marked only while its section spans the line 30% down the screen, so Trust and FAQ mark nothing.
- **How it works:**
  - A pinned scene from 1024 px: the illustrations move into one sticky frame, and the step in the middle shows its own.
  - It is a stacked list on phones, under reduced motion and without JavaScript.
  - Height went from 2,610 px to about 1,540 px.
- **Platform:**
  - One tabbed section (Inbox, Content Studio, Automations, Sales) replaces Features and the separate Content Studio section.
  - `#features` and `#content-studio` still land. Without JavaScript, every panel shows.
- **Book a demo:** `/demo/` saves a `DemoRequest`, emails sales, and has a honeypot and a limit of 5 an hour per address. It works without JavaScript and is in the sitemap.
- **Global illustrations:** varied names, $ amounts and "Paid by card" in every illustration on the page (the plan put this in Phase 2; done now because Phase 1 rebuilt these same illustrations).

- The proof strip says "Facebook Pages", not "Messenger", following messaging.md's rule that Facebook means a Page.

## Phase 2: global content and trust (built 2026-11-07)
Facts the code can't show were confirmed with you and recorded in `docs/ops/security-facts.md`:
- **Encryption:** the database disk is EBS-encrypted.
- **Backups:** running in production.
- **Region:** not published.
- **GDPR/DPA:** not offered yet.

The claim register points at that file.

- **Positioning line everywhere:**
  - It is the default page title and meta description (`SEO_DEFAULTS`) and the footer tagline. "For small businesses" is gone.
  - Lusaka, Zambia stays in the footer's company block.
  - The illustrations went global in Phase 1.
- **Trust section, two rows:**
  - **You stay in control:** your own number or domain, AI only when on, team roles, deletion on request.
  - **How your data is protected:** encryption, hosting, audit log, backups. Each opens its part of `/security/`.
- **`/security/`:**
  - It sits in the legal shell and its navigation, and is in the sitemap.
  - It covers:
    - hosting, without a region;
    - encryption: TLS with HSTS, EBS, encrypted backups, field-encrypted channel tokens;
    - access control: roles, two-step sign-in, read-only support access that is time-limited and audited;
    - the audit log: exactly what is recorded today;
    - backups and recovery;
    - incidents;
    - your data;
    - sub-processors;
    - reporting a vulnerability.
  - The status section appears only when `STATUS_PAGE_URL` is set. Every statement is in the claim register, and `/security/` is in the claims test.
- **`/.well-known/security.txt`** (RFC 9116), from `SECURITY_CONTACT_EMAIL`, else the support address. It returns 404 when there is neither.
- **Status page:** linked from both footers and the Security page only when `STATUS_PAGE_URL` is set.
- **Proof slots:**
  - Testimonials gained a logo and a case study, both editable in Operator Console → Testimonials.
  - The logo row, the hero's "Used by" logos and the case-study card show only for published testimonials that have a permission note. Nothing shows until then.
- **Meta Tech Provider:** not shown. It needs the status confirmed and Meta's partner wording checked.

### Needs you
- **Status page:** create a hosted status page (for example Better Stack or UptimeRobot) and set the `STATUS_PAGE_URL` secret.
- **Security contact:** set `SECURITY_CONTACT_EMAIL` (for example security@akilent.com) if reports shouldn't go to the support address.
- **Privacy policy's provider list is out of date.** It names Flutterwave and Stripe, but not PayPal (the live billing provider) or Cloudflare. The Security page lists what is used today, so the two pages disagree until the privacy policy is updated. That is legal text, so I left it for you.
- **Data region:** confirm it, so the Security page can name it.
