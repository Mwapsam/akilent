"""UI quality programme, Phase 6: automations and forms say what state they're in, and never
lose an edit quietly.

- Every Workflow.Status has its own label on the list, the editor and the stats page (stats
  called a paused automation a "Draft"), and edits waiting on a live one say so.
- The editor, the form builder and the post composer ran init() twice (x-init="init()" on top
  of Alpine calling it), so every edit started two saves; their own beforeunload listeners
  piled up on each boosted visit. The shared guard (app.js, akDirty) now covers leaving.
- A failed save says what happened and keeps the edits: the editor no longer calls every
  failure "offline", and Publish saves pending edits before it publishes the server's draft.
- A filtered list that's empty says nothing matches, not that nothing exists.
"""

import re
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.automation.models import Workflow
from apps.billing.models import Plan

TEMPLATES = Path(settings.BASE_DIR) / "templates"
STEPS = {"trigger": {"type": "manual"}, "steps": []}


@pytest.fixture
def owner(db, client):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    account = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    Plan.objects.create(slug="p", name="P", price_monthly=Decimal("1"))
    client.force_login(user)
    return account


def _wf(account, slug, status, draft=None):
    return Workflow.objects.create(
        account=account,
        name=slug.title(),
        slug=slug,
        status=status,
        definition=STEPS,
        draft_definition=draft,
    )


def _card(html, slug):
    return html.split(f'id="running-{slug}"', 1)[1].split('id="running-', 1)[0]


@pytest.mark.django_db
def test_the_list_labels_every_status_and_pending_edits(client, owner):
    S = Workflow.Status
    _wf(owner, "live", S.PUBLISHED)
    _wf(owner, "edited", S.PUBLISHED, draft=STEPS)
    _wf(owner, "held", S.PAUSED)
    _wf(owner, "gone", S.ARCHIVED)
    _wf(owner, "new", S.DRAFT)
    html = client.get("/automations/").content.decode()

    assert ">On<" in _card(html, "live")
    assert "Unpublished changes" not in _card(html, "live")
    edited = _card(html, "edited")
    assert "Unpublished changes" in edited and "Review changes" in edited
    assert ">Paused<" in _card(html, "held")
    assert ">Archived<" in _card(html, "gone")
    new = _card(html, "new")
    # A draft is off, not a warning, and says how to get it running.
    assert '<span class="badge badge-neutral ml-1">Draft</span>' in new
    assert "Finish and turn on" in new


@pytest.mark.django_db
def test_stats_page_calls_a_paused_automation_paused(client, owner):
    _wf(owner, "held", Workflow.Status.PAUSED)
    html = client.get("/automations/held/stats/").content.decode()
    assert "Paused" in html and "Draft" not in html


@pytest.mark.django_db
def test_a_filtered_runs_list_says_nothing_matches(client, owner):
    _wf(owner, "live", Workflow.Status.PUBLISHED)
    assert "Nobody here yet" in client.get("/automations/live/runs/").content.decode()
    filtered = client.get("/automations/live/runs/?status=failed").content.decode()
    assert "No customers match" in filtered and "Nobody here yet" not in filtered


@pytest.mark.django_db
def test_publish_saves_pending_edits_first(client, owner):
    _wf(owner, "live", Workflow.Status.PUBLISHED, draft=STEPS)
    html = client.get("/automations/live/").content.decode()
    publish = re.findall(r'<form[^>]*action="/automations/live/publish/"[^>]*>', html)
    assert publish and all("data-wf-publish" in f for f in publish)
    # The editor waits for its own save before letting the publish through.
    assert 'document.addEventListener("submit", this._onPublish, true)' in html
    # Its own resubmit after that save is let through (on the Code tab it used to loop).
    assert 'form.dataset.wfSaved = "1"' in html
    # No failure is called "offline" unless it is, and nothing promises a save it won't make.
    assert "Your changes will be saved when you reconnect" not in html


@pytest.mark.parametrize(
    "path, component",
    [
        ("automation/workflow_editor.html", "workflowEditor()"),
        ("conversations/form_detail.html", "formBuilder()"),
        ("publishing/post_new.html", "composer()"),
    ],
)
def test_editors_start_once_and_use_the_shared_leave_guard(path, component):
    """Alpine calls init() itself: x-init="init()" ran it twice."""
    src = (TEMPLATES / path).read_text(encoding="utf-8")
    assert f'x-data="{component}"' in src
    assert 'x-init="init()"' not in src
    assert 'addEventListener("beforeunload"' not in src


def test_no_template_runs_an_alpine_init_twice():
    offenders = [
        str(p.relative_to(TEMPLATES))
        for p in TEMPLATES.rglob("*.html")
        if re.search(
            r'x-data="\w+\([^"]*\)"[^>]*x-init="init\(\)"',
            p.read_text(encoding="utf-8"),
        )
    ]
    assert offenders == []


def test_the_composer_lets_go_of_its_window_listeners():
    js = (Path(settings.BASE_DIR) / "static" / "js" / "composer.js").read_text(
        encoding="utf-8"
    )
    assert 'addEventListener("beforeunload"' not in js
    assert 'window.removeEventListener("online", this._onOnline)' in js


BOOSTED = {
    "HTTP_HX_REQUEST": "true",
    "HTTP_HX_BOOSTED": "true",
    "HTTP_X_AKILENT_SHELL": "app",
}


@pytest.mark.django_db
def test_the_outcome_message_survives_the_full_page_load(client, owner):
    """Pause posts (boosted), redirects to the list, and htmx fetches the list. The list
    used to ship its own script and needed a real page load; it swaps in place now
    (templatePicker is in static/js/page_factories.js), and the "paused" message comes
    with that swap."""
    _wf(owner, "live", Workflow.Status.PUBLISHED)
    resp = client.post("/automations/live/pause/", **BOOSTED)
    assert resp.status_code == 302
    followed = client.get(resp["Location"], **BOOSTED)
    assert not followed.has_header("HX-Redirect")  # swapped in place
    page = followed.content.decode()
    assert 'id="flash-messages"' in page
    assert "is paused" in page.split('id="flash-messages"', 1)[1][:300]


@pytest.mark.django_db
def test_an_unknown_answers_filter_shows_everything_and_says_so(client, owner):
    from apps.conversations.models import ConversationForm

    form = ConversationForm.objects.create(account=owner, name="Sizes")
    html = client.get(
        f"/inbox/forms/{form.pk}/responses/?status=bogus"
    ).content.decode()
    assert "No answers here yet" in html and "No answers match" not in html
    assert 'href="?" class="tab tab-active"' in html


def test_editors_share_one_wording_for_failed_saves():
    """akFailure.draftSaveMessage (app.js) is the one kind -> message table for autosaves."""
    editor = (TEMPLATES / "automation/workflow_editor.html").read_text(encoding="utf-8")
    builder = (TEMPLATES / "conversations/form_detail.html").read_text(encoding="utf-8")
    for src in (editor, builder):
        assert "akFailure.draftSaveMessage(" in src and "window.akCsrf()" in src
        assert "csrftoken=" not in src
    # One form save at a time is tracked by its own flag, not by saveState (which an edit
    # during the save resets to "dirty").
    assert "if (this._saving) { this._timer" in builder


def test_a_left_composer_stops_its_timers():
    js = (Path(settings.BASE_DIR) / "static" / "js" / "composer.js").read_text(
        encoding="utf-8"
    )
    destroy = js.split("destroy: function () {", 1)[1].split("},", 1)[0]
    assert (
        "clearInterval(this._genPoll)" in destroy
        and "clearTimeout(this._undoTimer)" in destroy
    )
