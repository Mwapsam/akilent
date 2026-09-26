# Pilot readiness checklist (before inviting business 3)

Run this on production after the Operational Readiness changes are deployed. Every check says
what to do and what counts as a pass. Record the date and result next to each one.

Run commands on the VPS in the deploy directory. `dc` means `docker compose --profile prod`.

## A. Server

- [ ] **Gunicorn, not the development server.**
  `dc logs web | grep -i -E "gunicorn|development server" | tail -3`.
  **Pass:** "Starting gunicorn" / "Booting worker", and no "Starting development server".
- [ ] **DEBUG is off.** Open `https://akilent.com/this-page-does-not-exist`.
  **Pass:** a plain 404 page, not a yellow Django debug page.
- [ ] **Internal ports are closed.** From another machine (not the VPS):
  `nc -zv -w 3 <server-ip> 5432 6379 5672 15672 8000`.
  **Pass:** every port refused or timed out.
- [ ] **Health check.** `curl -s https://akilent.com/healthz`.
  **Pass:** `{"ok": true, "db": true, "cache": true}`.
- [ ] **Restart recovery.** `sudo reboot`, wait 2 minutes, open the site.
  **Pass:** it loads, and `dc ps` shows every service `Up` (web `healthy`).
- [ ] **Uptime monitor.** An external monitor (UptimeRobot, Better Stack…) checks
  `https://akilent.com/healthz` every 5 minutes and alerts you.
  **Pass:** a test alert reaches your phone.
- [ ] **Sentry.** The `SENTRY_DSN` secret is set.
  **Pass:** Sentry shows the `production` environment receiving events.

## B. Backups

- [ ] The backup bucket, IAM user and secrets are set up (`docs/ops/backups.md`).
- [ ] `dc run --rm backup dump`.
  **Pass:** `db/<today>.dump` appears in the bucket.
- [ ] `dc run --rm backup restore-test`.
  **Pass:** "restore test passed" with non-zero account and conversation counts.
- [ ] `/manage/pilot/` Backups panel.
  **Pass:** shows the dump and "Passed".

## C. Failure drills

- [ ] **Webhook recovery.**
  1. `dc stop web nginx`.
  2. From a personal phone, send 5 numbered WhatsApp messages ("test 1" … "test 5") to a pilot
     business number.
  3. Wait 2 minutes, then `dc start web nginx`.

  **Pass:** within about 15 minutes (Meta retries with backoff), all 5 appear in that inbox
  **exactly once**, in order.
- [ ] **Durable waits.**
  1. Create a draft test workflow: keyword "drilltest" → wait 1 minute → reply "wait worked".
     Turn it on.
  2. Message "drilltest", then immediately `dc restart celery_worker celery_beat`.

  **Pass:** "wait worked" arrives about 1 to 2 minutes later, once. Turn the workflow off
  afterwards.
- [ ] **AI isolation** (only if AI is on for a business).
  1. `dc stop celery_ai`.
  2. Reply from the inbox and trigger an automation.
  3. Ask for an AI suggestion.
  4. `dc start celery_ai`.

  **Pass:** the reply and the automation send at once while AI is stopped; the suggestion appears
  after the AI worker is back.
- [ ] **Queues drain.** `dc exec rabbitmq rabbitmqctl list_queues name messages consumers`.
  **Pass:** every queue has at least 1 consumer and `messages` near 0, including `scheduler` and
  `automation`. Schedule an email "send later" for 2 minutes' time; it goes out.
- [ ] **Workers visible.** `/manage/pilot/` Workers panel.
  **Pass:** every queue seen under 2 minutes ago.
- [ ] **Team notification email.** Trigger a workflow with "Notify the team".
  **Pass:** the email arrives, and its link opens `https://akilent.com/...`, not localhost.

## D. Meta and legal

- [ ] **Legal pages.** Replace every `[PLACEHOLDER]` in `templates/legal/`, then have a lawyer
  review the pages.
- [ ] **Meta App Dashboard → Settings → Basic:**
  - Privacy Policy URL `https://akilent.com/privacy/`;
  - Terms of Service URL `https://akilent.com/terms/`;
  - User data deletion → Data Deletion Instructions URL `https://akilent.com/data-deletion/`.
- [ ] **Business verification** is approved in Meta Business Settings → Security Centre.
  **Pass:** Akilent's own number sends again (error 131037 gone).
- [ ] **Advanced Access / Tech Provider.** App Review → Permissions shows **Advanced Access** for
  `whatsapp_business_management` and `whatsapp_business_messaging`, and Tech Provider onboarding
  is complete. Without these, Embedded Signup can't connect other businesses.

## E. Per new pilot business

- [ ] They connect WhatsApp themselves through Embedded Signup. Watch; don't take over.
- [ ] `/manage/pilot/` shows them, "Measuring, day 1 of 7".
- [ ] Book the one-week interview. Ask:
  - What was the first thing you tried to do?
  - What confused you?
  - What saved you the most time?
  - What did you expect Akilent to do that it didn't?
  - Did you ever stop trusting it? Why?

  File each answer under exactly one of: confusion, missing capability, reliability,
  performance, nice-to-have.
- [ ] Check the Pilot Command Center every morning. A **Quiet** badge (3 days with no customer
  message) means call them.
