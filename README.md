# Akilent

Akilent is a **customer engagement and communications platform for businesses**.

It helps businesses capture customer conversations, respond consistently, follow up with interested customers, keep their teams informed, and automate routine customer engagement through WhatsApp and email.

The core idea is simple:

> **Never lose a customer conversation, and always know what should happen next.**

Akilent combines a shared customer inbox, contacts, conversations, deterministic automations, team notifications, and communication infrastructure in one system.

AI can be introduced later to remove specific sources of friction, but **Akilent's core customer-engagement workflows do not depend on AI**.

---

## What Akilent Does

### Customer Conversations

Akilent provides a centralized view of customer conversations and the actions taken around them.

* Shared customer conversations
* Conversation history
* Customer/contact profiles
* Conversation activity
* Customer attributes and custom fields
* Conversation status and ownership
* Customer communication history
* WhatsApp messaging
* Email communication infrastructure
* Conversation-linked customer engagement

Conversations are the center of the system.

Customer activity can lead to automation, customer records, follow-ups, team notifications, and eventually revenue attribution.

---

## Customer Engagement Automations

Akilent allows businesses to automate common customer-engagement jobs without requiring them to understand workflow engines.

Instead of configuring technical triggers and actions, business users start with a goal such as:

* **Answer customer questions**
* **Never miss a customer**
* **Keep your team informed**

Examples include:

### Answer customer questions

* Answer pricing questions
* Tell customers where the business is located
* Welcome new customers
* Offer customers a menu of common options

### Never miss a customer

* Follow up with interested customers
* Check in with quiet customers
* Respond when the business is closed

### Keep your team informed

* Hand new interested customers to the team
* Notify teammates when customer engagement requires attention

Behind the scenes, these simple experiences are implemented using Akilent's deterministic workflow engine.

Business users do not need to understand workflow IDs, step IDs, JSON, trigger types, or execution state.

---

## Deterministic Automation Engine

Akilent contains a durable workflow engine for executing customer-engagement automations.

The engine supports concepts including:

* Business-event triggers
* Conversation events
* Customer lifecycle events
* Lead lifecycle events
* Manual enrollment
* Conditional execution
* Delays and waiting
* Reply-based continuation
* Buttons and interactive menus
* WhatsApp template messages
* Conversation replies
* Customer attribute updates
* Lead creation
* Lead assignment
* Team notifications
* Workflow activity and execution history
* Durable workflow runs
* Step-level execution tracking
* Retry-safe execution

Workflows are persisted and executed independently of the web request that started them.

Waiting workflows can be resumed when their scheduled condition becomes due or when a matching customer reply arrives.

The automation engine is deliberately deterministic. A business can run its core customer-engagement processes without an AI model.

---

## Automation Transparency

Automation is not intended to be a black box.

Akilent provides explanations for both actions and non-actions.

### What happened?

Automation activity can show what Akilent did in response to a customer conversation.

For example:

```text
Customer: How much is your service?

Answer pricing questions
✓ Replied with pricing information

Track interested customer
✓ Marked customer as interested

Tell your team
✓ Samuel was notified
```

### Why didn't it reply?

Akilent can explain why an automation did not run.

Examples include:

* Customer message did not match the automation
* Automation is paused
* Customer has already received a response within the configured period
* WhatsApp reply window is closed
* Customer has opted out
* Required message template is not approved
* Required customer information is missing
* Automation is not turned on

The execution engine records structured outcomes and reason codes, while a separate explanation layer converts them into business-friendly language.

---

## Personalization

Automation messages can safely use customer and business information.

Supported sources include:

* Customer first name
* Customer full name
* Customer phone number
* Business name
* Customer custom fields
* Workflow context
* Explicit fixed text

Customer-specific values are resolved when the message is sent.

For missing customer information, automations can use a per-customer fallback or explain why a message could not be sent.

This prevents a customer's information from accidentally being reused for another customer.

---

## WhatsApp Business

Akilent integrates with the official **Meta WhatsApp Cloud API** for customer conversations and messaging.

Supported capabilities include:

* Inbound WhatsApp messages
* Outbound WhatsApp messages
* Two-way conversations
* Approved WhatsApp message templates
* 24-hour customer-service messaging window
* Interactive buttons
* Interactive lists
* Media messages
* WhatsApp webhooks
* Message status handling
* Opt-in and opt-out consent
* Per-number rate limiting
* Meta error handling
* Template synchronization
* Failure monitoring and alerts

WhatsApp functionality is controlled by the `WHATSAPP_ENABLED` configuration flag.

See:

`docs/whatsapp-operations.md`

for operational details.

---

## Email

Akilent also provides transactional email infrastructure.

The email backend is pluggable through `MailProviderSettings` or environment configuration.

Supported providers include:

* **AWS SES**
* **Stalwart**

Email capabilities include:

* Sending-domain configuration
* DNS verification
* Provider verification
* Transactional email delivery
* Delivery tracking
* Bounce and complaint handling
* Delivery logs
* Usage tracking
* Email notifications

For AWS SES, Akilent supports:

* Easy DKIM domain identities
* SESv2 sending
* SNS bounce/complaint ingestion

Pending domains are periodically rechecked by the `reverify_pending_domains` Celery task.

---

## Contacts and Customer Records

Akilent maintains customer records alongside their conversations.

Contacts can contain:

* Name
* Phone number
* Communication information
* Custom attributes
* Conversation history
* Engagement activity
* Lead information

Customer records can be created and updated through conversations and customer-management interfaces.

System fields and custom customer attributes are intentionally treated separately so that automation personalization can safely distinguish between customer identity and business-defined fields.

---

## Leads and Customer Interest

Customer conversations can become business opportunities.

Akilent supports a conversation-first lead lifecycle:

```text
Conversation
    ↓
Customer shows interest
    ↓
Lead created
    ↓
Lead qualified / lost
    ↓
Team assignment
    ↓
Team notification
```

Leads are connected to the conversations that produced them where attribution is available.

Lead automation can:

* Track a customer as interested
* Update interest information
* Assign a teammate
* Notify the team
* Update customer information
* React to lifecycle changes

The goal is to prevent interested customers from disappearing after the initial conversation.

---

## Team Collaboration

Akilent supports businesses where customer conversations need to be shared across a team.

Features include:

* Shared conversations
* Team members
* Conversation assignment
* Lead assignment
* Internal notifications
* Customer activity history
* Automation activity
* Email notifications to relevant team members

Automation can notify the appropriate team members when human involvement is required.

---

## Automation Management

Business users interact with automations through goals rather than technical workflow definitions.

The primary automation experience provides:

### Goal gallery

```text
What do you want Akilent to help with?

Answer customer questions
Never miss a customer
Keep your team informed
```

### Simple configuration

Automations are configured using:

```text
WHEN
customer asks about pricing

DO
send pricing reply

THEN
optionally tag customer
optionally tell my team
```

### Automation states

Automations have simple operational states:

* **Draft**
* **On**
* **Paused**

Pausing an automation stops execution without creating a new workflow version.

### Preview and testing

Users can preview customer-facing messages before enabling an automation and can send supported test messages to their own WhatsApp number.

---

## Automation Readiness

Before an automation is enabled, Akilent checks prerequisites that can prevent it from working.

Examples include:

* WhatsApp connection
* Required message template approval
* Required configuration
* Business communication settings

Akilent distinguishes between something being **unable to work** and something simply **not having run yet**.

For example:

```text
✓ Ready to run

No customers have triggered this automation yet.
```

rather than treating zero activity as an error.

---

## Automation Activity and Health

Akilent records workflow execution using durable workflow and step-run records.

The business-facing interface surfaces this as:

* Last run
* Customers helped
* Successful actions
* Failed actions
* Automation activity
* Customer-specific activity
* Failure explanations

Technical execution details remain available to staff/developer interfaces without being exposed unnecessarily to business users.

---

## Architecture

Akilent is implemented as a multi-tenant Django application.

### Core stack

* **Django 6**
* **PostgreSQL**
* **Celery**
* **Redis**
* **Tailwind CSS**
* **Alpine.js**
* **Flutterwave**
* **Meta WhatsApp Cloud API**
* **AWS SES / Stalwart**

The application is server-rendered using Django templates.

The automation engine, communication providers, customer records, conversations, and billing infrastructure are implemented as separate application domains within the Django project.

---

## Multi-Tenancy

Akilent is designed for multiple businesses to operate independently within the same platform.

Business-owned data and communication resources are scoped to the appropriate account.

This includes:

* Contacts
* Conversations
* Automations
* Workflow runs
* Message templates
* Email domains
* Usage
* Billing
* Team members
* Communication settings

---

## Billing and Usage

Akilent includes subscription and usage infrastructure using Flutterwave.

Current infrastructure includes:

* Subscription billing
* Plan-based quotas
* Usage tracking
* Payment callbacks
* Billing state
* Communication usage accounting

Flutterwave test-mode cards are documented separately for development/testing environments.

---

## Frontend / Static Assets

The UI is server-rendered Django templates styled with compiled **Tailwind CSS** and self-hosted **Alpine.js**.

Node/npm is not required.

Tailwind is compiled using the standalone CLI.

One-time setup:

```bash
# Windows x64
curl -L -o tools/tailwindcss.exe \
  https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-windows-x64.exe
```

Build the stylesheet:

```bash
tools/tailwindcss.exe \
  -i assets/app.css \
  -o static/css/app.css \
  --minify
```

Or run the watcher:

```bash
tools/tailwindcss.exe \
  -i assets/app.css \
  -o static/css/app.css \
  --watch
```

The compiled stylesheet and vendored JavaScript/fonts are committed to the repository so the application can run without the Tailwind CLI being installed.

In production:

```bash
python manage.py collectstatic
```

collects and cache-busts static assets through WhiteNoise.

---

# Production

Start the production stack with:

```bash
docker compose --profile prod up
```

---

## Required Encryption Key

`FIELD_ENCRYPTION_KEY` must be configured.

The application will raise an error during startup if it is missing.

Generate one with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## Email Provider Configuration

The mail backend can be configured using the `MailProviderSettings` singleton or environment variables.

Relevant environment variables are documented in:

```text
.env.example
```

Supported backends:

```text
AWS SES
Stalwart
```

Email domains are verified using Akilent's DNS verification service and, for SES, the provider's own verification status.

---

## Development

Run the Django application using the project's normal development configuration.

Before committing changes, run the relevant application tests.

The project contains tests covering areas including:

* Conversations
* Contacts
* WhatsApp
* Automations
* Workflow execution
* Template personalization
* Lead lifecycle
* Assignment
* Team notifications
* Attribution
* Email
* Billing
* UI rendering

---

# Current Product Status

Akilent's current product focus is **customer engagement through conversations and deterministic automation**.

The system is moving toward pilot validation with real businesses.

The current pilot gate is to validate that a non-technical business owner can independently:

1. Find a useful automation.
2. Configure it.
3. Preview the customer-facing message.
4. Send themselves a test where supported.
5. Turn the automation on.
6. Have a real customer conversation trigger it.
7. See what Akilent did.
8. Understand why an automation did not act when it should not have.
9. Pause and resume the automation.
10. Understand the automation without needing to understand the underlying workflow engine.

The pilot is intended to validate **how businesses use the existing capabilities**, rather than introduce unfinished functionality.

---

# Roadmap

The product roadmap is intentionally staged.

### Current

**Customer engagement foundation**

* Conversations
* Contacts
* WhatsApp
* Email infrastructure
* Deterministic automation
* Customer follow-up
* Lead lifecycle
* Team collaboration
* Automation transparency
* Automation testing and readiness

### After pilot validation

Potential areas include:

* More flexible recipe-based automation
* Additional automation triggers
* Structured lead-source reporting
* Recovered-revenue reporting
* Revenue attribution
* Response-speed analytics
* Email inbound conversations
* Additional communication channels

### Later

The longer-term direction includes:

* Optional AI-assisted automation
* Social messaging channels
* Marketing campaigns
* Content calendars
* Marketing operations
* Revenue intelligence

AI is intended to be an **optional capability layered onto the deterministic system**, rather than a prerequisite for core automation.

---

# Product Principle

Akilent's automation engine can be sophisticated internally while remaining simple for the business owner.

The owner should think:

> **"I want Akilent to answer pricing questions."**

not:

> **"I need to configure a `conversation.message_received` trigger with a `send_whatsapp` action and a workflow step ID."**

The platform translates the first into the second.

That separation is fundamental to Akilent's design.
