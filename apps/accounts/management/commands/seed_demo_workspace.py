"""Management command: build (or rebuild) the labelled demo workspace for sales demos.

Usage
-----
    poetry run python manage.py seed_demo_workspace
    poetry run python manage.py seed_demo_workspace --password <pw>
    poetry run python manage.py seed_demo_workspace --allow-prod   # only on purpose

What it creates
---------------
One retailer workspace, "Demo Boutique (demo workspace)", walking the journey in
docs/sales/demo.md: Content Studio posts with tracked chat links and clicks, WhatsApp
and Instagram conversations that came from them, leads, orders (some paid), a
follow-up and an unanswered enquiry.

Every record is synthetic:
- phone numbers are +1 202 555-01xx: 555-0100 to 555-0199 are reserved for fiction;
- emails use example.com, and the demo logins use the reserved .invalid domain;
- media are the PNG illustrations in static/img/demo/.

Conversations, leads and orders go through the same services real traffic uses, so
lead capture, post attribution and post results are the product's own behaviour.
Workflows are not enrolled and nothing is sent: the workspace has no WhatsApp number,
no live Instagram/Facebook connection and no payment provider. The "published" posts
carry no Meta ids and metrics collection is switched off for them.

Running it again deletes the demo workspace (and only it) and builds it fresh.

Exit codes
----------
    0 — built
    1 — refused (not DEBUG without --allow-prod, or the demo slug belongs to someone else)
"""

import secrets
import sys
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

DEMO_SLUG = "akilent-demo-workspace"
DEMO_NAME = "Demo Boutique (demo workspace)"
OWNER_EMAIL = "demo.owner@akilent-demo.invalid"
MEMBER_EMAIL = "demo.team@akilent-demo.invalid"
DEMO_NOTE = "Demo workspace (seed_demo_workspace): every feature on for sales demos."


class Command(BaseCommand):
    help = "Build or rebuild the labelled demo workspace (synthetic data only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            help="Password for the demo logins (default: a random one, printed).",
        )
        parser.add_argument(
            "--allow-prod",
            action="store_true",
            default=False,
            help="Run even though DEBUG is off.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["allow_prod"]:
            self.stderr.write(
                "Refusing: DEBUG is off. Pass --allow-prod to build the demo workspace here."
            )
            sys.exit(1)
        if not self._remove_previous():
            sys.exit(1)

        password = options["password"] or secrets.token_urlsafe(12)
        with transaction.atomic():
            account, owner = self._workspace(password)
            posts = self._content(account)
            self._journey(account, owner, posts)

        self.stdout.write(self.style.SUCCESS(f"Demo workspace ready: {DEMO_NAME}"))
        self.stdout.write(f"  Owner login:    {OWNER_EMAIL}")
        self.stdout.write(f"  Teammate login: {MEMBER_EMAIL}")
        self.stdout.write(f"  Password:       {password}")
        self.stdout.write("  Script:         docs/sales/demo.md")

    # --- Workspace ----------------------------------------------------------

    def _remove_previous(self) -> bool:
        """Delete the previous demo workspace, but never an account it didn't create."""
        from apps.accounts.models import Account

        account = Account.objects.filter(slug=DEMO_SLUG).first()
        if account is not None:
            owners = set(account.memberships.values_list("user__email", flat=True))
            if not owners or not owners <= {OWNER_EMAIL, MEMBER_EMAIL}:
                self.stderr.write(
                    f"Refusing: account '{DEMO_SLUG}' has members that aren't the demo logins."
                )
                return False
            account.delete()
        User.objects.filter(email__in=(OWNER_EMAIL, MEMBER_EMAIL)).delete()
        return True

    def _workspace(self, password):
        from apps.accounts.models import Account, Membership
        from apps.billing import api as billing_api
        from apps.billing import features as catalog
        from apps.billing.models import Subscription

        account = Account.objects.create(
            company_name=DEMO_NAME,
            slug=DEMO_SLUG,
            selected_services=Account.Services.BOTH,
            selected_channels=["email", "facebook", "instagram", "whatsapp"],
            onboarding_state=Account.Onboarding.COMPLETED,
            industry="Retail",
            city="Lusaka",
            country="Zambia",
        )
        owner = User.objects.create_user(
            OWNER_EMAIL, OWNER_EMAIL, password, first_name="Demo", last_name="Owner"
        )
        member = User.objects.create_user(
            MEMBER_EMAIL, MEMBER_EMAIL, password, first_name="Tendai", last_name="Demo"
        )
        Membership.objects.create(
            user=owner, account=account, role=Membership.Role.OWNER
        )
        Membership.objects.create(
            user=member, account=account, role=Membership.Role.MEMBER
        )

        # Every sellable feature on through overrides: no plan is touched.
        for feature in catalog.FEATURES:
            if (
                catalog.overridable(feature.key)
                and feature.availability == catalog.SELLABLE
            ):
                billing_api.set_override(
                    account, feature.key, grant=True, note=DEMO_NOTE
                )
        # Keep the demo usable for a month after each rebuild.
        Subscription.objects.filter(account=account).update(
            status=Subscription.TRIALING,
            trial_ends_at=timezone.now() + timedelta(days=30),
        )
        return account, owner

    # --- Attract: Content Studio --------------------------------------------

    def _content(self, account) -> dict:
        from apps.publishing.links import DEFAULT_PREFILL, new_code
        from apps.publishing.models import (
            ContentCampaign,
            ContentPillar,
            ContentPlanItem,
            ContentPost,
            ContentPostVariant,
            LinkClick,
            TrackedLink,
        )

        now = timezone.now()
        today = timezone.localdate()
        arrivals = ContentPillar.objects.create(account=account, name="New arrivals")
        ContentPillar.objects.create(
            account=account, name="Behind the scenes", color="#0ea5e9"
        )
        summer = ContentCampaign.objects.create(
            account=account,
            name="Summer linen",
            objective="Sell the new linen range through WhatsApp orders.",
            start_date=today - timedelta(days=10),
            end_date=today + timedelta(days=20),
        )
        for days, topic in (
            (2, "Styling the linen dress three ways"),
            (5, "Meet the tailor"),
            (9, "Weekend sale reminder"),
        ):
            ContentPlanItem.objects.create(
                account=account,
                pillar=arrivals,
                campaign=summer,
                topic=topic,
                platforms=["instagram", "facebook"],
                media_type=ContentPost.MediaType.IMAGE,
                planned_date=today + timedelta(days=days),
                plan_status=ContentPlanItem.PlanStatus.PLANNED,
            )

        def image(name):
            return f"{settings.STATIC_URL}img/demo/{name}"

        def published(caption, media, platforms, days_ago, clicks):
            at = now - timedelta(days=days_ago)
            post = ContentPost.objects.create(
                account=account,
                platforms=platforms,
                media_type=ContentPost.MediaType.IMAGE,
                caption=caption,
                media_urls=[media],
                status=ContentPost.Status.PUBLISHED,
                scheduled_at=at,
                published_at=at,
                brief_objective="Get WhatsApp enquiries for the new range.",
                brief_cta="Tap the link to chat on WhatsApp.",
            )
            links = {}
            for platform in platforms:
                variant = ContentPostVariant.objects.create(
                    post=post,
                    platform=platform,
                    caption=caption,
                    status=ContentPostVariant.Status.PUBLISHED,
                    scheduled_at=at,
                    published_at=at,
                    metrics_latest=_metrics(platform),
                    metrics_collected_at=now - timedelta(hours=3),
                    metrics_next_at=None,  # never ask Meta about a demo post
                )
                link = TrackedLink.objects.create(
                    account=account,
                    post=post,
                    variant=variant,
                    campaign=summer,
                    code=new_code(),
                    wa_number="12025550100",
                    prefill_text=DEFAULT_PREFILL,
                )
                LinkClick.objects.bulk_create(
                    LinkClick(link=link, visitor_hash=uuid.uuid4().hex)
                    for _ in range(clicks[platform])
                )
                links[platform] = link
            return links

        dress = published(
            "Our linen summer dress is here: breathable, made locally, sizes S to XL.",
            image("linen-dress.png"),
            ["instagram", "facebook"],
            days_ago=6,
            clicks={"instagram": 41, "facebook": 17},
        )
        sandals = published(
            "Hand-stitched leather sandals, built to last all summer.",
            image("leather-sandals.png"),
            ["instagram"],
            days_ago=2,
            clicks={"instagram": 23},
        )
        ContentPost.objects.create(
            account=account,
            platforms=["instagram", "facebook"],
            media_type=ContentPost.MediaType.IMAGE,
            caption="Weekend sale: 15% off the linen range, Saturday and Sunday only.",
            media_urls=[image("linen-dress.png")],
            status=ContentPost.Status.DRAFT,
        )
        return {"dress": dress, "sandals": sandals}

    # --- Engage, convert --------------------------------------------------------

    def _journey(self, account, owner, posts):
        from apps.commerce.models import Payment
        from apps.commerce.services import create_order, mark_paid
        from apps.core.actions import run_action
        from apps.crm.models import Lead
        from apps.crm.services import create_lead, set_lead_status

        now = timezone.now()
        dress_ig, dress_fb = posts["dress"]["instagram"], posts["dress"]["facebook"]
        sandals_ig = posts["sandals"]["instagram"]

        def ref(link):
            return f"{link.prefill_text} (ref {link.code})"

        def order(contact, conversation, name, price, *, paid):
            created = create_order(
                account,
                contact,
                [{"product": None, "name": name, "unit_price": price, "quantity": 1}],
                conversation_id=conversation.public_id,
            )
            if paid:
                payment = Payment.objects.create(
                    account=account,
                    order=created,
                    provider="demo",
                    amount=created.total,
                    currency=created.currency,
                )
                mark_paid(payment, transaction_id=f"demo-{uuid.uuid4().hex[:12]}")
            return created

        def lead(contact, conversation, source, status=None):
            found = create_lead(
                account,
                contact,
                source=source,
                conversation_id=conversation.public_id,
            )
            if status:
                set_lead_status(found, status, actor=owner)
            return found

        # 1. Instagram post link → WhatsApp chat → lead → paid order.
        thandi, chat = self._whatsapp(
            account,
            "+12025550101",
            "Thandi",
            "Banda",
            [
                (5, ref(dress_ig)),
                (5, "How much is the linen dress in size M?"),
            ],
        )
        self._reply(chat, "Hi Thandi! It's $45 and we have size M in stock.", 5)
        lead(thandi, chat, "Instagram post", Lead.Status.QUALIFIED)
        order(thandi, chat, "Linen summer dress (M)", Decimal("45.00"), paid=True)

        # 2. Facebook post link → WhatsApp chat → lead → order waiting for payment.
        joseph, chat = self._whatsapp(
            account,
            "+12025550102",
            "Joseph",
            "Mwale",
            [(4, ref(dress_fb)), (4, "Do you have it in blue? I want two.")],
        )
        self._reply(
            chat, "We do! Two blue dresses come to $90. Shall I send a payment link?", 4
        )
        lead(joseph, chat, "Facebook post", Lead.Status.CONTACTED)
        order(
            joseph, chat, "Linen summer dress, blue (x2)", Decimal("90.00"), paid=False
        )

        # 3. Second post → WhatsApp chat → paid order.
        grace, chat = self._whatsapp(
            account,
            "+12025550103",
            "Grace",
            "Phiri",
            [(1, ref(sandals_ig)), (1, "I want the sandals in size 39, can I order?")],
        )
        self._reply(chat, "Yes! Size 39 is $60. Here's how to pay.", 1)
        lead(grace, chat, "Instagram post", Lead.Status.QUALIFIED)
        order(grace, chat, "Leather sandals (39)", Decimal("60.00"), paid=True)

        # 4. Instagram DM → lead with a follow-up due tomorrow.
        chipo, chat = self._instagram(
            account, "chipo.style", "Chipo", "Is the linen dress still available?", 1
        )
        self._reply(chat, "It is! Which size would you like?", 1)
        lead(chipo, chat, "Instagram DM")
        run_action(
            "create_followup",
            {"account": account},
            conversation=chat,
            due_at=now + timedelta(days=1),
            note="Chipo asked about the dress. Check her size and send a payment link.",
            created_by=owner,
        )

        # 5. A question nobody has answered yet, with no post behind it.
        self._whatsapp(
            account,
            "+12025550104",
            "Peter",
            "Zulu",
            [(0, "What time do you open on Saturday?")],
        )

    def _whatsapp(self, account, phone, first, last, messages):
        """A customer's WhatsApp messages, recorded the way the webhook records them."""
        from apps.contacts.models import Contact
        from apps.conversations.services import record_inbound_whatsapp_message
        from apps.whatsapp.models import Conversation as WhatsAppConversation
        from apps.whatsapp.models import MessageLog, WhatsAppContact

        contact = Contact.objects.create(
            account=account,
            phone=phone,
            first_name=first,
            last_name=last,
            email=f"{first.lower()}.{last.lower()}@example.com",
            source="whatsapp",
        )
        wa_contact = WhatsAppContact.objects.create(
            account=account, phone_number=phone, contact=contact
        )
        wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
        conversation = None
        for days_ago, body in messages:
            log = MessageLog.objects.create(
                account=account,
                conversation=wa_conversation,
                contact=wa_contact,
                message_id=f"wamid.demo.{uuid.uuid4().hex}",
                direction=MessageLog.Direction.INBOUND,
                message_type=MessageLog.MessageType.TEXT,
                content=body,
                status=MessageLog.Status.DELIVERED,
                timestamp=timezone.now() - timedelta(days=days_ago, minutes=30),
            )
            conversation = (
                record_inbound_whatsapp_message(
                    contact=contact,
                    wa_contact=wa_contact,
                    whatsapp_conversation=wa_conversation,
                    message_log=log,
                    enroll_workflows=False,
                )
                or conversation
            )
        return contact, conversation

    def _instagram(self, account, username, first, body, days_ago):
        """An Instagram DM on a demo account that isn't connected (nothing can be sent)."""
        from apps.contacts.models import Contact
        from apps.conversations.services import record_inbound_instagram_message
        from apps.instagram.models import InstagramConversation
        from apps.instagram.models.account import InstagramBusinessAccount
        from apps.instagram.models.contact import InstagramContact
        from apps.instagram.models.message import InstagramMessage

        ig_account = InstagramBusinessAccount.objects.create(
            account=account,
            instagram_business_account_id=f"demo_{uuid.uuid4().hex[:10]}",
            username="demoboutique",
            name=DEMO_NAME,
            access_token="",
            verify_token=secrets.token_hex(8),
            is_active=False,
        )
        contact = Contact.objects.create(
            account=account, first_name=first, source="instagram"
        )
        ig_contact = InstagramContact.objects.create(
            account=account,
            instagram_scoped_id=f"demo_{uuid.uuid4().hex[:12]}",
            contact=contact,
        )
        at = timezone.now() - timedelta(days=days_ago, minutes=30)
        thread = InstagramConversation.objects.create(
            instagram_account=ig_account,
            instagram_contact=ig_contact,
            last_message_at=at,
            last_inbound_at=at,
        )
        message = InstagramMessage.objects.create(
            conversation=thread,
            message_id=f"mid.demo.{uuid.uuid4().hex}",
            direction=InstagramMessage.Direction.INBOUND,
            body=body,
            status=InstagramMessage.Status.DELIVERED,
            timestamp=at,
            metadata={"demo": True, "username": username},
        )
        conversation = record_inbound_instagram_message(
            contact=contact,
            instagram_conversation=thread,
            instagram_message=message,
            enroll_workflows=False,
        )
        return contact, conversation

    def _reply(self, conversation, body, days_ago):
        """The business's answer, as the inbox records a sent reply."""
        from apps.conversations.services import record_outbound_message

        record_outbound_message(
            conversation=conversation,
            body=body,
            timestamp=timezone.now() - timedelta(days=days_ago, minutes=20),
            status="delivered",
            metadata={"demo": True},
        )


def _metrics(platform: str) -> dict:
    """Illustrative post numbers for every metric the channel reports, shown as collected."""
    from apps.publishing.metrics import metrics_for

    keys = [m.key for m in metrics_for(platform, "image")]
    base = 1840 if platform == "instagram" else 960
    values = {key: max(base // (2**i), 3) for i, key in enumerate(keys)}
    return {
        "values": values,
        "states": dict.fromkeys(values, "available"),
        "error": "",
    }
