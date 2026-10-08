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


def message(c, direction, body, sent_by=None):
    return Message.objects.create(
        account=c.account,
        conversation=c,
        direction=direction,
        body=body,
        timestamp=timezone.now(),
        metadata={"sent_by": sent_by} if sent_by else {},
    )


@pytest.mark.django_db
def test_a_reply_after_the_customer_asked_something_else_isnt_offered(account):
    """'Do you deliver?' then 'What's your number?': a reply now may answer the second one."""
    contact = Contact.objects.create(account=account, phone="+260971000002")
    c = Conversation.objects.create(
        account=account,
        contact=contact,
        channel="whatsapp",
        last_message_at=timezone.now(),
    )
    q = message(c, Message.Direction.INBOUND, "Do you deliver?")
    p = AIProposal.objects.create(
        account=account,
        conversation=c,
        trigger_message=q,
        action="handoff",
        status="ready",
    )
    message(c, Message.Direction.INBOUND, "And what's your phone number?")
    message(c, Message.Direction.OUTBOUND, "It's 0977 123 456.")
    assert ai_api.learnable_answer(p, c) is None


@pytest.mark.django_db
def test_customer_details_are_kept_out_of_saved_answers(client, account):
    c, p = convo_with_handoff(account)
    Message.objects.filter(conversation=c, direction="outbound").update(
        body="Yes! Call me on 0977123456 or jane@example.com"
    )
    learn = ai_api.serialize(p, c)["learn"]
    assert learn["answer"] == "Yes! Call me on [phone] or [email]"

    login(client, account)
    client.post(
        f"/inbox/{c.public_id}/ai/learn/",
        {
            "proposal": p.pk,
            "question": "Q?",
            "answer": "Ring +260 97 7123456 any time.",
        },
    )
    assert KnowledgeBaseEntry.objects.get().content == "Ring [phone] any time."


# ---- import edge cases ------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_questions_ai_is_drafting_are_labelled_drafted_by_ai(client, account):
    login(client, account)
    questions = "\n".join(f"Question number {i}?" for i in range(35))
    client.post("/settings/ai/knowledge/import/", {"text": questions})
    origins = list(
        KnowledgeBaseEntry.objects.order_by("pk").values_list("origin", flat=True)
    )
    assert origins == ["ai_draft"] * 30 + ["import"] * 5

    AISettings.objects.filter(account=account).update(enabled=False)
    client.post("/settings/ai/knowledge/import/", {"text": "Is parking free?"})
    assert KnowledgeBaseEntry.objects.get(title="Is parking free?").origin == "import"


@pytest.mark.django_db
def test_drafting_never_overwrites_an_answer_the_owner_wrote_meanwhile(
    monkeypatch, account
):
    entry = KnowledgeBaseEntry.objects.create(
        account=account,
        title="Is it free?",
        content="",
        origin="ai_draft",
        is_active=False,
    )
    draft = AIDraft.objects.create(
        account=account,
        kind="knowledge_answers",
        prompt="x",
        context={"entry_ids": [entry.pk]},
    )

    class OwnerIsFaster(Fake):
        def chat(self, *a, **k):
            # While the model thinks, the owner writes and approves an answer.
            KnowledgeBaseEntry.objects.filter(pk=entry.pk).update(
                content="Yes, always.", is_active=True, reviewed_at=timezone.now()
            )
            return CompletionResult(
                text=json.dumps({"answers": [{"id": entry.pk, "answer": "AI text"}]}),
                model="fake-k",
            )

    monkeypatch.setattr(
        "apps.ai.providers.get_ai_provider", lambda *a, **k: OwnerIsFaster()
    )
    run(draft)
    entry.refresh_from_db()
    assert entry.content == "Yes, always." and draft.result["answered"] == 0


# ---- reading a website ------------------------------------------------------------------------


def resolves_to(ip):
    return lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
    ]


def page(body, status=200, location=None, content_type="text/html; charset=utf-8"):
    r = MagicMock(status_code=status)
    r.is_redirect = location is not None
    r.headers = {"content-type": content_type}
    if location:
        r.headers["location"] = location
    data = body if isinstance(body, bytes) else body.encode()
    r.iter_content.return_value = [data]
    return r


def site(monkeypatch, pages, ip="93.184.216.34"):
    """``pages`` maps a URL to ``page()`` arguments. Returns the URLs fetched, with the IP used."""
    monkeypatch.setattr(knowledge.socket, "getaddrinfo", resolves_to(ip))
    fetched = []

    def open_(url, *, ip, accept):
        fetched.append((url, ip))
        return page(*pages[url]) if url in pages else page("", 404)

    monkeypatch.setattr(knowledge, "_open", open_)
    return fetched


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"])
def test_internal_addresses_are_refused(monkeypatch, ip):
    fetched = site(monkeypatch, {}, ip=ip)
    with pytest.raises(knowledge.WebsiteError, match="public website"):
        knowledge.read_site("https://intranet.example")
    assert fetched == []


def test_a_redirect_into_the_network_is_refused(monkeypatch):
    def getaddrinfo(host, port, *a, **k):
        ip = "10.0.0.1" if host == "internal.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    fetched = site(
        monkeypatch, {"https://shop.example": ("", 302, "http://internal.example/")}
    )
    monkeypatch.setattr(knowledge.socket, "getaddrinfo", getaddrinfo)
    with pytest.raises(knowledge.WebsiteError, match="public website"):
        knowledge.read_site("https://shop.example")
    assert not any("internal" in url for url, _ip in fetched)


def test_the_connection_goes_to_the_address_that_was_checked(monkeypatch):
    """DNS rebinding: the host is never resolved again after the check."""
    calls = {}

    class Session:
        trust_env = True

        def mount(self, prefix, adapter):
            calls["sni"] = adapter._host

        def get(self, url, **kw):
            calls.update(url=url, headers=kw["headers"], trust_env=self.trust_env)
            return page("<p>hi</p>")

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(knowledge.requests, "Session", Session)
    resp = knowledge._open(
        "https://shop.example:8443/faq?x=1", ip="93.184.216.34", accept="text/html"
    )
    assert calls["url"] == "https://93.184.216.34:8443/faq?x=1"
    assert calls["headers"]["Host"] == "shop.example:8443"
    assert calls["sni"] == "shop.example"
    assert calls["trust_env"] is False  # no environment proxies
    resp.close()
    assert calls["closed"]


def test_responses_are_closed_even_on_errors(monkeypatch):
    responses = []

    def open_(url, *, ip, accept):
        r = page("", 500)
        responses.append(r)
        return r

    monkeypatch.setattr(knowledge.socket, "getaddrinfo", resolves_to("93.184.216.34"))
    monkeypatch.setattr(knowledge, "_open", open_)
    with pytest.raises(knowledge.WebsiteError):
        knowledge._get("https://shop.example")
    assert responses[0].close.called


def test_reading_a_site_follows_its_own_links_and_plain_text_robots(monkeypatch):
    fetched = site(
        monkeypatch,
        {
            "https://shop.example/robots.txt": (
                "User-agent: *\nDisallow: /admin",
                200,
                None,
                "text/plain",
            ),
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
    urls = [url for url, _ip in fetched]
    assert "https://shop.example/admin" not in urls
    assert not any("other.example" in u or u.endswith(".pdf") for u in urls)


def test_a_site_that_redirects_to_www_is_read_beyond_its_first_page(monkeypatch):
    fetched = site(
        monkeypatch,
        {
            "https://example.com": ("", 301, "https://www.example.com/"),
            "https://www.example.com/robots.txt": (
                "User-agent: *\nDisallow: /private",
                200,
                None,
                "text/plain",
            ),
            "https://www.example.com/": (
                '<p>Home</p><a href="/faq">FAQ</a><a href="/private">P</a>',
            ),
            "https://www.example.com/faq": ("<p>Answers</p>",),
        },
    )
    pages = knowledge.read_site("example.com")
    assert [url for url, _text in pages] == [
        "https://www.example.com/",
        "https://www.example.com/faq",
    ]
    assert "https://www.example.com/private" not in [u for u, _ip in fetched]


@pytest.mark.parametrize(
    "body,content_type",
    [
        ("<p>From €20 — “fresh”</p>".encode(), "text/html"),
        ('<meta charset="utf-8"><p>From €20 — “fresh”</p>'.encode(), "text/html"),
        (
            "<p>From €20 — “fresh”</p>".encode("cp1252"),
            "text/html; charset=windows-1252",
        ),
    ],
)
def test_pages_are_decoded_in_their_own_charset(monkeypatch, body, content_type):
    site(monkeypatch, {"https://shop.example": (body, 200, None, content_type)})
    assert knowledge.read_site("https://shop.example")[0][1] == "From €20 — “fresh”"


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
    assert draft.context == {"url": "https://shop.example"}  # pages aren't kept
    entry = KnowledgeBaseEntry.objects.get(account=account)
    assert entry.origin == "website" and entry.source_url == "https://shop.example"
    assert entry.needs_review and not entry.is_active
    assert "Is delivery free? Yes, over K500." in Fake.calls[-1]["user"]


@pytest.mark.django_db
def test_a_retry_after_a_model_timeout_doesnt_read_the_site_again(monkeypatch, account):
    from apps.ai.providers import AIProviderError

    fetched = site(monkeypatch, {"https://shop.example": ("<p>Open daily.</p>",)})
    draft = AIDraft.objects.create(
        account=account,
        kind="website",
        prompt="x",
        context={"url": "https://shop.example"},
    )

    class Flaky(Fake):
        tries = 0

        def chat(self, *a, **k):
            Flaky.tries += 1
            if Flaky.tries == 1:
                raise AIProviderError("timeout")
            return CompletionResult(text='{"entries": []}', model="fake-k")

    monkeypatch.setattr("apps.ai.providers.get_ai_provider", lambda *a, **k: Flaky())
    from celery.exceptions import Retry

    with pytest.raises(Retry):  # the model times out: the task asks Celery to retry
        build_draft.apply(args=(draft.pk,), throw=True)
    draft.refresh_from_db()
    assert draft.status == "pending" and len(draft.context["pages"]) == 1
    run(draft)  # the retry
    assert draft.status == "ready", draft.error
    assert Flaky.tries == 2
    assert [u for u, _ip in fetched].count("https://shop.example") == 1


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
