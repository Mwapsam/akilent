"""Every claim on the landing page is registered, and every registered claim has evidence.

The register is the table in docs/marketing/messaging.md (section 5). A sentence that describes
what Akilent does carries ``data-claim="<id> ..."``; this test fails when the page uses an id the
register doesn't have, when a product statement carries no id at all, or when a claim's evidence
(``path::text``) has gone from the code. See the guide for how to add a claim.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.conf import settings

from apps.billing.models import Plan

GUIDE = Path(settings.BASE_DIR) / "docs" / "marketing" / "messaging.md"
_ROW = re.compile(
    r"^\|\s*([a-z][a-z-]*)\s*\|\s*(Outcome|Capability|Mechanism)\s*\|[^|]*\|\s*([^|]+?)\s*\|\s*$"
)

# Sections whose paragraphs, list items and FAQ answers are product statements.
CHECKED_SECTIONS = {"how-it-works", "features", "trust", "faq", "developers"}


def _register() -> dict[str, tuple[str, str]]:
    rows = {}
    for line in GUIDE.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match:
            rows[match.group(1)] = (match.group(2), match.group(3))
    return rows


class _Claims(HTMLParser):
    """Collects claim ids, and product statements in CHECKED_SECTIONS that carry none."""

    VOID = {
        "meta",
        "link",
        "img",
        "br",
        "input",
        "hr",
        "source",
        "path",
        "polyline",
        "line",
        "circle",
        "rect",
    }

    def __init__(self):
        super().__init__()
        self.stack = []  # (tag, attrs)
        self.ids = set()
        self.untagged = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-claim" in attrs:
            self.ids.update(attrs["data-claim"].split())
        # A classed <li> is a card container (its <p> carries the claim); an eyebrow is a label.
        is_statement = tag in ("p", "details") or (
            tag == "li" and not attrs.get("class")
        )
        if "eyebrow" in (attrs.get("class") or "").split():
            is_statement = False
        if is_statement and self._in_checked_section() and not self._covered(attrs):
            self.untagged.append(f"<{tag}> in #{self._section()}")
        if tag not in self.VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def _section(self):
        for tag, attrs in reversed(self.stack):
            if tag == "section":
                return attrs.get("id")
        return None

    def _in_checked_section(self):
        return self._section() in CHECKED_SECTIONS

    def _covered(self, attrs):
        chain = [attrs] + [a for _, a in reversed(self.stack)]
        for a in chain:
            if "data-claim" in a or "data-claim-exempt" in a:
                return True
            if (
                "section-head" in (a.get("class") or "").split()
                or a.get("class") == "plans"
            ):
                return True  # section intros and plan cards (built from the catalog)
        return False


@pytest.fixture
def plans(db):
    Plan.objects.create(slug=Plan.TRIAL, name="Trial", price_monthly=0, trial_days=14)
    Plan.objects.create(slug=Plan.PROFESSIONAL, name="Professional", price_monthly=49)


def test_register_evidence_still_exists():
    register = _register()
    assert len(register) >= 20, "couldn't read the claim register table"
    missing = []
    for claim_id, (claim_type, evidence) in register.items():
        if claim_type == "Outcome":
            continue
        path, _, text = evidence.partition("::")
        source = Path(settings.BASE_DIR) / path
        if not source.exists() or text not in source.read_text(encoding="utf-8"):
            missing.append(f"{claim_id}: {evidence}")
    assert not missing, (
        "claims whose evidence is gone (update the copy or the register):\n"
        + "\n".join(missing)
    )


@pytest.mark.django_db
@pytest.mark.parametrize("whatsapp", [True, False])
def test_every_claim_on_the_page_is_registered(client, settings, plans, whatsapp):
    settings.WHATSAPP_ENABLED = whatsapp
    html = client.get("/").content.decode()
    parser = _Claims()
    parser.feed(html)

    unknown = parser.ids - set(_register())
    assert not unknown, (
        f"data-claim ids missing from docs/marketing/messaging.md: {sorted(unknown)}"
    )
    assert not parser.untagged, (
        "product statements without data-claim (tag them, or mark a disclaimer data-claim-exempt): "
        + ", ".join(parser.untagged)
    )


@pytest.mark.django_db
def test_inbox_is_not_claimed_for_channels_that_do_not_arrive_there(
    client, settings, plans
):
    """Only WhatsApp messages reach the shared inbox today (see the guide, section 3)."""
    settings.WHATSAPP_ENABLED = True
    text = re.sub(r"<[^>]+>", " ", client.get("/").content.decode())
    text = re.sub(r"\s+", " ", text).lower()
    for phrase in (
        "email conversations",
        "other channels",
        "omnichannel",
        "every channel",
        "never miss a customer",
        "never lose a customer",
    ):
        assert phrase not in text, phrase
