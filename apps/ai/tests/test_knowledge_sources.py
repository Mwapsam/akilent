"""Adding knowledge besides writing it by hand: import, review, the inbox, a website.

Whatever is imported, drafted by AI or read from a website waits under "Needs review" and AI never
sees it until a person approves it.
"""

import json
import socket
from unittest.mock import MagicMock

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.ai import api as ai_api
from apps.ai import facts as business_facts
from apps.ai import knowledge
from apps.ai.models import AIDraft, AIProposal, AISettings, KnowledgeBaseEntry
from apps.ai.providers.base import AIProvider, CompletionResult
from apps.ai.tasks import build_draft
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message

TASKCENTRO = """How do I create a TaskCentro account?
How do I create my freelancer profile?
How do I add my services?
Does TaskCentro charge freelancers?"""


class Fake(AIProvider):
    answer, calls = "", []

    def chat(self, messages, system="", max_tokens=1024, temperature=0.3, timeout=None):
        Fake.calls.append({"system": system, "user": messages[-1].content})
        return CompletionResult(text=Fake.answer, model="fake-k")


@pytest.fixture(autouse=True)
def fake(settings, monkeypatch):
    settings.AI_PROVIDER_BACKEND = "ollama"
    monkeypatch.setattr("apps.ai.providers.get_ai_provider", lambda *a, **k: Fake())
    Fake.answer, Fake.calls = "", []
    cache.clear()


@pytest.fixture
def account(db):
    account = Account.objects.create(company_name="TaskCentro")
    AISettings.objects.create(
        account=account, enabled=True, business_notes="Joining is free for freelancers."
    )
    return account


def login(client, account, role=Membership.Role.OWNER, name="owner"):
    user = User.objects.create_user(name, f"{name}@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=role)
    client.force_login(user)
    return user


def run(draft):
    build_draft.apply(args=(draft.pk,))
    draft.refresh_from_db()
    return draft


# ---- parsing ----------------------------------------------------------------------------------


def test_a_plain_list_of_questions_is_questions_without_answers():
    assert knowledge.parse_pasted(TASKCENTRO) == [
        ("How do I create a TaskCentro account?", ""),
        ("How do I create my freelancer profile?", ""),
        ("How do I add my services?", ""),
        ("Does TaskCentro charge freelancers?", ""),
    ]


def test_question_and_answer_blocks_and_markers():
    text = """1. How do I sign up?
Go to taskcentro.com and
confirm your email.

Q: Is it free?
A: Yes, joining is free.
Q: How do I get paid?
A: Through the app."""
    assert knowledge.parse_pasted(text) == [
        ("How do I sign up?", "Go to taskcentro.com and confirm your email."),
        ("Is it free?", "Yes, joining is free."),
        ("How do I get paid?", "Through the app."),
    ]


def test_csv_with_a_header_and_duplicates():
    data = b"question,answer\nIs it free?,Yes.\nis it free?,Again\nHow do I join?,\n"
    assert knowledge.parse_csv(data) == [
        ("Is it free?", "Yes."),
        ("How do I join?", ""),
    ]


# ---- import and review ------------------------------------------------------------------------


@pytest.mark.django_db
def test_imported_questions_wait_for_review_and_ai_drafts_the_answers(client, account):
    login(client, account)
    client.post(
        "/settings/ai/knowledge/import/",
        {"text": "Is it free?\nYes, joining is free.\n\n" + TASKCENTRO},
    )
    entries = KnowledgeBaseEntry.objects.filter(account=account).order_by("pk")
    assert entries.count() == 5
    assert not any(e.is_active for e in entries) and all(
        e.needs_review for e in entries
    )
    assert (
        entries[0].origin == "import" and entries[0].content == "Yes, joining is free."
    )
    blank = [e for e in entries if not e.content]
    assert {e.origin for e in blank} == {"ai_draft"} and len(blank) == 4
    # AI never sees an entry before it's approved.
    assert business_facts.build(account)["knowledge"] == []

    draft = AIDraft.objects.get(kind="knowledge_answers")
    assert draft.context == {"entry_ids": [e.pk for e in blank]}
    charge = next(e for e in blank if "charge" in e.title)
    Fake.answer = json.dumps(
        {
            "answers": [
                {"id": charge.pk, "answer": "No, joining is free."},
                {"id": blank[0].pk, "answer": ""},
                {"id": 999999, "answer": "Not one of ours."},
            ]
        }
    )
    run(draft)
    assert draft.status == "ready" and draft.result == {"answered": 1, "unanswered": 3}
    charge.refresh_from_db()
    assert charge.content == "No, joining is free." and not charge.is_active
    # Drafted from what the business already told AI.
    assert "Joining is free for freelancers." in Fake.calls[-1]["system"]


@pytest.mark.django_db
def test_approving_turns_entries_on(client, account):
    login(client, account)
    answered = KnowledgeBaseEntry.objects.create(
        account=account, title="Q1", content="A1", origin="import", is_active=False
    )
    blank = KnowledgeBaseEntry.objects.create(
        account=account, title="Q2", content="", origin="ai_draft", is_active=False
    )
    # The on/off switch can't skip review.
    client.post(f"/settings/ai/knowledge/{answered.pk}/toggle/")
    answered.refresh_from_db()
    assert not answered.is_active

    client.post("/settings/ai/knowledge/approve-all/")
    answered.refresh_from_db()
    blank.refresh_from_db()
    assert answered.is_active and not answered.needs_review
    assert blank.needs_review and not blank.is_active  # no answer yet

    client.post(
        f"/settings/ai/knowledge/{blank.pk}/edit/",
        {"title": "Q2", "content": "Written by me.", "approve": "1"},
    )
    blank.refresh_from_db()
    assert blank.is_active and blank.reviewed_at and blank.content == "Written by me."
    assert {e["title"] for e in business_facts.build(account)["knowledge"]} == {
        "Q1",
        "Q2",
    }


@pytest.mark.django_db
def test_csv_upload_and_the_page(client, account):
    login(client, account)
    upload = SimpleUploadedFile(
        "faq.csv", b"question,answer\nDo you deliver?,Yes.\n", content_type="text/csv"
    )
    client.post("/settings/ai/knowledge/import/", {"file": upload})
    entry = KnowledgeBaseEntry.objects.get(account=account)
    assert entry.title == "Do you deliver?" and entry.needs_review
    html = client.get("/settings/ai/knowledge/").content.decode()
    assert "Needs review" in html and "Do you deliver?" in html
    assert "Import from your website" in html


@pytest.mark.django_db
def test_only_owners_and_admins_import(client, account):
    login(client, account, role=Membership.Role.MEMBER, name="agent")
    client.post("/settings/ai/knowledge/import/", {"text": TASKCENTRO})
    assert not KnowledgeBaseEntry.objects.exists()


@pytest.mark.django_db
def test_background_kinds_cant_be_started_from_the_draft_endpoint(client, account):
    login(client, account, role=Membership.Role.MEMBER, name="agent")
    r = client.post("/ai/drafts/", {"kind": "website", "prompt": "https://x.example"})
    assert r.status_code == 400 and not AIDraft.objects.exists()


# ---- learning from the inbox ------------------------------------------------------------------


def convo_with_handoff(account, sent_by=None):
    contact = Contact.objects.create(account=account, phone="+260971000001")
    c = Conversation.objects.create(
        account=account,
        contact=contact,
        channel="whatsapp",
        last_message_at=timezone.now(),
    )
    q = Message.objects.create(
        account=account,
        conversation=c,
        direction=Message.Direction.INBOUND,
        body="Can I list cleaning services? Call me on +260971234567",
        timestamp=timezone.now(),
    )
    p = AIProposal.objects.create(
        account=account,
        conversation=c,
        trigger_message=q,
        action="handoff",
        payload={"note": "x"},
        status="ready",
    )
    Message.objects.create(
        account=account,
        conversation=c,
        direction=Message.Direction.OUTBOUND,
        body="Yes, cleaning is one of our categories.",
        timestamp=timezone.now(),
        metadata={"sent_by": sent_by} if sent_by else {},
    )
    return c, p


@pytest.mark.django_db
def test_a_team_reply_can_be_saved_as_an_answer(client, account):
    c, p = convo_with_handoff(account)
    learn = ai_api.serialize(p, c)["learn"]
    assert learn == {
        "question": "Can I list cleaning services? Call me on [phone]",
        "answer": "Yes, cleaning is one of our categories.",
        "saved": False,
    }

    login(client, account)
    r = client.post(
        f"/inbox/{c.public_id}/ai/learn/",
        {
            "proposal": p.pk,
            "question": "Can I list cleaning services?",
            "answer": learn["answer"],
        },
    )
    assert r.status_code == 200 and r.json()["proposal"]["learn"]["saved"] is True
    entry = KnowledgeBaseEntry.objects.get(account=account)
    assert entry.origin == "inbox" and entry.is_active and not entry.needs_review
    assert entry.title == "Can I list cleaning services?"

    again = client.post(
        f"/inbox/{c.public_id}/ai/learn/",
        {"proposal": p.pk, "question": "x", "answer": "y"},
    )
    assert again.status_code == 400


@pytest.mark.django_db
def test_an_ai_reply_is_nothing_to_learn(account):
    c, p = convo_with_handoff(account, sent_by="ai")
    assert ai_api.serialize(p, c)["learn"] is None


@pytest.mark.django_db
def test_agents_cant_change_what_ai_knows(client, account):
    c, p = convo_with_handoff(account)
    login(client, account, role=Membership.Role.MEMBER, name="agent")
    r = client.post(
        f"/inbox/{c.public_id}/ai/learn/",
        {"proposal": p.pk, "question": "q", "answer": "a"},
    )
    assert r.status_code == 403 and not KnowledgeBaseEntry.objects.exists()


# ---- reading a website ------------------------------------------------------------------------


def resolves_to(ip):
    return lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
    ]


def page(html, status=200, location=None):
    r = MagicMock(status_code=status, encoding="utf-8")
    r.is_redirect = location is not None
    r.headers = {"content-type": "text/html; charset=utf-8"}
    if location:
        r.headers["location"] = location
    r.raw.read.return_value = html.encode()
    return r


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"])
def test_internal_addresses_are_refused(monkeypatch, ip):
    monkeypatch.setattr(knowledge.socket, "getaddrinfo", resolves_to(ip))
    with pytest.raises(knowledge.WebsiteError, match="public website"):
        knowledge.read_site("https://intranet.example")


def test_a_redirect_into_the_network_is_refused(monkeypatch):
    def getaddrinfo(host, port, *a, **k):
        ip = "10.0.0.1" if host == "internal.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(knowledge.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(
        knowledge.requests,
        "get",
        lambda url, **k: (
            page("", 404)
            if url.endswith("robots.txt")
            else page("", 302, "http://internal.example/")
        ),
    )
    with pytest.raises(knowledge.WebsiteError, match="public website"):
        knowledge.read_site("https://shop.example")


def site(monkeypatch, pages):
    monkeypatch.setattr(knowledge.socket, "getaddrinfo", resolves_to("93.184.216.34"))
    fetched = []

    def get(url, **k):
        fetched.append(url)
        return page(*pages[url]) if url in pages else page("", 404)

    monkeypatch.setattr(knowledge.requests, "get", get)
    return fetched


def test_reading_a_site_follows_its_own_links_and_robots(monkeypatch):
    fetched = site(
        monkeypatch,
        {
            "https://shop.example/robots.txt": ("User-agent: *\nDisallow: /admin",),
            "https://shop.example": (
                "<html><head><title>x</title><script>var a=1</script></head><body>"
                "<nav>Menu</nav><h1>Welcome</h1><p>We deliver in Lusaka.</p>"
                '<a href="/faq#top">FAQ</a><a href="/admin">Admin</a>'
                '<a href="https://other.example/">Elsewhere</a><a href="/menu.pdf">PDF</a>'
                "</body></html>",
            ),
            "https://shop.example/faq": ("<p>Is delivery free? Yes, over K500.</p>",),
        },
    )
    pages = knowledge.read_site("shop.example")
    assert pages == [
        (
            "https://shop.example",
            "Welcome\nWe deliver in Lusaka.\nFAQ Admin Elsewhere PDF",
        ),
        ("https://shop.example/faq", "Is delivery free? Yes, over K500."),
    ]
    assert "https://shop.example/admin" not in fetched
    assert not any("other.example" in u or u.endswith(".pdf") for u in fetched)


@pytest.mark.django_db
def test_what_ai_finds_on_the_website_waits_for_review(client, monkeypatch, account):
    site(
        monkeypatch,
        {"https://shop.example": ("<p>Is delivery free? Yes, over K500.</p>",)},
    )
    login(client, account)
    client.post("/settings/ai/knowledge/website/", {"url": "shop.example"})
    draft = AIDraft.objects.get(kind="website")
    assert draft.context == {"url": "https://shop.example"}
    Fake.answer = json.dumps(
        {
            "entries": [
                {
                    "question": "Is delivery free?",
                    "answer": "Yes, for orders over K500.",
                    "url": "https://shop.example",
                },
                {"question": "No answer here", "answer": ""},
            ]
        }
    )
    run(draft)
    assert draft.status == "ready", draft.error
    assert draft.result == {"added": 1, "pages": 1}
    entry = KnowledgeBaseEntry.objects.get(account=account)
    assert entry.origin == "website" and entry.source_url == "https://shop.example"
    assert entry.needs_review and not entry.is_active
    assert "Is delivery free? Yes, over K500." in Fake.calls[-1]["user"]


@pytest.mark.django_db
def test_an_unreachable_site_fails_the_draft_with_a_reason(monkeypatch, account):
    monkeypatch.setattr(knowledge.socket, "getaddrinfo", resolves_to("10.1.1.1"))
    draft = AIDraft.objects.create(
        account=account,
        kind="website",
        prompt="x",
        context={"url": "https://x.example"},
    )
    run(draft)
    assert draft.status == "error" and "public website" in draft.error
