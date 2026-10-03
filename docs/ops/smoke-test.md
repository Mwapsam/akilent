# Akilent Launch-Day Smoke Test

**Run this immediately after every production deployment, and always after the go-live deploy.**

**Time required:** ~30 minutes  
**Who runs it:** On-call operator  
**Prerequisite:** You have a test business account on production (separate from any pilot customers)

---

## 1. Infrastructure

| Step | Action | Expected result | Pass? |
|---|---|---|---|
| 1.1 | `curl -f https://akilent.com/healthz` | `{"ok":true,"db":true,"cache":true,"beat":true}` | |
| 1.2 | Open https://akilent.com in browser | Page loads over HTTPS, no certificate warning | |
| 1.3 | Check SSL: `echo | openssl s_client -connect akilent.com:443 2>/dev/null \| openssl x509 -noout -dates` | Certificate is valid and not expiring within 30 days | |
| 1.4 | `docker compose ps` on VPS | All containers show `Up` | |
| 1.5 | `/healthz` includes `"beat": true` | Beat is alive | |

---

## 2. Authentication

| Step | Action | Expected result | Pass? |
|---|---|---|---|
| 2.1 | Open https://akilent.com/auth/login/ | Login page loads | |
| 2.2 | Submit wrong password | "Invalid credentials" error, no stack trace | |
| 2.3 | Log in with the test account | Dashboard loads | |
| 2.4 | Click logout | Session ends, redirected to login | |
| 2.5 | Request a password reset | Email received within 2 minutes | |
| 2.6 | Log in again | Works | |

---

## 3. Tenant Isolation (quick check)

| Step | Action | Expected result | Pass? |
|---|---|---|---|
| 3.1 | While logged in as Test Account, note a contact ID | e.g., `con_abc123` | |
| 3.2 | Log in as a different test account (if available) | Separate dashboard | |
| 3.3 | Try to access `https://akilent.com/contacts/con_abc123` directly | 404 or redirected to own dashboard | |

---

## 4. Core Workflow

Run this end-to-end with the test account:

| Step | Action | Expected result | Pass? |
|---|---|---|---|
| 4.1 | Create a new Contact | Contact appears in contact list | |
| 4.2 | Start a Conversation for that contact | Conversation thread opens | |
| 4.3 | Send a reply in the conversation | Message shows in thread | |
| 4.4 | Create a Lead from the conversation | Lead appears in pipeline | |
| 4.5 | Advance the Lead stage | Stage updates correctly | |
| 4.6 | Create an Order from the conversation | Order appears in commerce | |
| 4.7 | Create a Follow-up on the conversation | Follow-up scheduled | |

---

## 5. WhatsApp (if enabled)

| Step | Action | Expected result | Pass? |
|---|---|---|---|
| 5.1 | Send a test WhatsApp message to the production number | Message appears in conversation thread | |
| 5.2 | Reply from the Akilent inbox | Message delivered to the test phone | |
| 5.3 | Check delivery status | Shows "delivered" or "read" | |
| 5.4 | Check the WhatsApp number health in Operator Console | Status: connected | |

---

## 6. Email

| Step | Action | Expected result | Pass? |
|---|---|---|---|
| 6.1 | Send a test email to a verified address | Email received within 2 minutes | |
| 6.2 | Check SES sending stats in AWS Console | No bounce/complaint spikes | |
| 6.3 | Send to an SES bounce simulator address (`bounce@simulator.amazonses.com`) | Bounce handled; address added to suppression list | |

---

## 7. AI (if enabled)

| Step | Action | Expected result | Pass? |
|---|---|---|---|
| 7.1 | Open a conversation, check for AI suggestion | Suggestion appears (if AI is on for the account) | |
| 7.2 | Check that `AI_AUTONOMY_ENABLED=True` is set | Autopilot is not force-disabled | |
| 7.3 | Set `AI_AUTONOMY_ENABLED=False` in the environment, redeploy | Autopilot stops firing; suggestions still work | |
| 7.4 | Restore `AI_AUTONOMY_ENABLED=True` | Autopilot resumes | |

---

## 8. Operations

| Step | Action | Expected result | Pass? |
|---|---|---|---|
| 8.1 | Open Operator Console `/manage/` | Loads, shows worker heartbeats | |
| 8.2 | Check all queues in the console | No queues showing as "late" | |
| 8.3 | Check backup status in the console | Last backup: today's date (or last night's) | |
| 8.4 | Check `restore-check-latest.json` in S3 | Last restore test: "passed" | |
| 8.5 | Trigger a test Sentry event | Appears in Sentry within 1 minute | |
| 8.6 | Post a test Slack message via the webhook | Appears in `#ops` | |
| 8.7 | Check Sentry traces sample rate | `SENTRY_TRACES_SAMPLE_RATE` > 0 for performance monitoring | |

---

## 9. SEO and Public Pages

| Step | Action | Expected result | Pass? |
|---|---|---|---|
| 9.1 | `curl https://akilent.com/robots.txt` | Does NOT start with `Disallow: /` (indexing is on) | |
| 9.2 | `curl https://akilent.com/sitemap.xml` | Valid XML with public page URLs | |
| 9.3 | Open a public page and view source | `<link rel="canonical">` present | |
| 9.4 | Open a logged-in dashboard page and view source | `<meta name="robots" content="noindex">` present | |

---

## 10. Legal Pages

| Step | Action | Expected result | Pass? |
|---|---|---|---|
| 10.1 | Open https://akilent.com/privacy/ | Privacy Policy loads | |
| 10.2 | Open https://akilent.com/terms/ | Terms of Service loads | |
| 10.3 | Open https://akilent.com/cookies/ | Cookie Policy loads | |
| 10.4 | Open https://akilent.com/data-deletion/ | Data deletion page loads | |

---

## Result

| Section | Pass / Fail / N/A |
|---|---|
| Infrastructure | |
| Authentication | |
| Tenant isolation | |
| Core workflow | |
| WhatsApp | |
| Email | |
| AI | |
| Operations | |
| SEO | |
| Legal | |

**Date:** _______________  
**Run by:** _______________  
**Overall result:** GO / NO-GO  
**Notes:** _______________

---

## If any step fails

- Do not declare GO.
- Investigate and resolve before announcing the launch.
- If a step is marked N/A because the feature is not in pilot scope, document that explicitly.
