# On-Call Ownership

> **Fill this in before the pilot launch.** This document is referenced by every incident runbook.

## Primary contact

| Role | Name | Slack | Phone |
|---|---|---|---|
| On-call operator | _[name]_ | _[@handle]_ | _[number]_ |
| Backup | _[name]_ | _[@handle]_ | _[number]_ |

## Escalation path

1. On-call operator responds within 15 minutes
2. If no response: backup operator
3. If no response: [founder/CTO name]

## What "on-call" means for the pilot

- You receive Slack `#ops` alerts on your phone
- You respond to alerts within 15 minutes during business hours, 30 minutes outside
- You follow the relevant incident runbook
- You record what happened

## Alert channels

| Alert | Destination |
|---|---|
| Application/infra alerts | Slack `#ops` |
| WhatsApp failure spikes | Slack `#ops` |
| Email failure spikes | Slack `#ops` |
| Backup failures | Slack `#ops` |
| Uptime monitor | Slack `#ops` (configure in UptimeRobot/Better Stack) |

## Incident runbooks

- [INC-001 Application outage](incidents/INC-001-application-outage.md)
- [INC-002 Database failure](incidents/INC-002-database-failure.md)
- [INC-003 WhatsApp outage](incidents/INC-003-whatsapp-outage.md)
- [INC-004 Email outage](incidents/INC-004-email-outage.md)
- [INC-005 Bad deployment](incidents/INC-005-bad-deployment.md)
- [INC-006 Security incident](incidents/INC-006-security-incident.md)
- [INC-007 Queue/worker failure](incidents/INC-007-queue-worker-failure.md)
