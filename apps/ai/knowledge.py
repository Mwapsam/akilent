"""Ways to add to a business's knowledge base besides writing an entry by hand.

* **Import**: paste questions and answers (or just questions), or upload a CSV of
  ``question,answer``. Questions without an answer get one drafted by AI from what the business
  has already told it, or are left for the owner to answer.
* **Website**: read up to ``MAX_PAGES`` public pages of the business's site and let AI pull out
  the questions customers ask and their answers.

Everything added here starts **off**, under "Needs review": a person approves each entry before
AI may use it (``KnowledgeBaseEntry.needs_review``). A typo or an outdated web page never becomes
an automatic answer on its own.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

import requests

from apps.ai.proposals import ProposalError, extract_json
from apps.ai.types import ChatMessage

logger = logging.getLogger(__name__)

MAX_IMPORT = 100
MAX_QUESTION = 200
MAX_ANSWER = 2000
MAX_DRAFTED = 30  # questions sent to AI in one draft

MAX_PAGES = 10
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PAGE_TEXT = 8000
MAX_SITE_TEXT = 30000
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 8
USER_AGENT = "AkilentKnowledgeImport/1.0 (+reads pages the business asked it to)"

_BULLET = re.compile(r"^\s*(?:[-*•]|\d{1,3}[.)])\s+")
_Q = re.compile(r"^\s*(?:q|question)\s*[:.)-]\s*", re.I)
_A = re.compile(r"^\s*(?:a|answer)\s*[:.)-]\s*", re.I)


# ---- parsing --------------------------------------------------------------------------------


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def parse_pasted(text: str) -> list[tuple[str, str]]:
    """``[(question, answer)]`` from pasted text; ``answer`` is "" for a question on its own.

    Understands blocks separated by blank lines (first line the question, the rest its answer),
    "Q: ... / A: ..." markers, and a plain list of questions, one per line.
    """
    pairs: list[tuple[str, str]] = []
    blocks = re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").strip())
    for block in blocks:
        lines = [_BULLET.sub("", ln).strip() for ln in block.split("\n")]
        lines = [ln for ln in lines if ln]
        if not lines:
            continue
        if any(_Q.match(ln) for ln in lines):
            question, answer = "", []  # type: tuple[str, list[str]]
            for ln in lines:
                if _Q.match(ln):
                    if question:
                        pairs.append((question, _clean(" ".join(answer))))
                    question, answer = _Q.sub("", ln), []
                else:
                    answer.append(_A.sub("", ln))
            if question:
                pairs.append((question, _clean(" ".join(answer))))
        elif len(lines) > 1 and all(ln.endswith("?") for ln in lines):
            pairs += [(ln, "") for ln in lines]
        else:
            pairs.append(
                (lines[0], _clean(" ".join(_A.sub("", ln) for ln in lines[1:])))
            )
    return _tidy(pairs)


def parse_csv(data: bytes) -> list[tuple[str, str]]:
    """``[(question, answer)]`` from a CSV whose first column is the question, second the answer.
    A header row ("question", "answer") is skipped."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    pairs = []
    for i, row in enumerate(csv.reader(io.StringIO(text))):
        if not row or not row[0].strip():
            continue
        if i == 0 and row[0].strip().lower() in ("question", "questions", "q"):
            continue
        pairs.append((row[0], row[1] if len(row) > 1 else ""))
    return _tidy(pairs)


def _tidy(pairs) -> list[tuple[str, str]]:
    out, seen = [], set()
    for q, a in pairs:
        q, a = _clean(q)[:MAX_QUESTION], (a or "").strip()[:MAX_ANSWER]
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        out.append((q, a))
    return out[:MAX_IMPORT]


# ---- creating entries -----------------------------------------------------------------------


def add_for_review(account, pairs, *, origin: str, source_url: str = "") -> list:
    """Create an inactive entry per new question (one the knowledge base doesn't already have).
    Returns the entries created."""
    from apps.ai.models import KnowledgeBaseEntry

    have = {
        t.lower()
        for t in KnowledgeBaseEntry.objects.filter(account=account).values_list(
            "title", flat=True
        )
    }
    created = []
    for question, answer in pairs:
        if question.lower() in have:
            continue
        have.add(question.lower())
        created.append(
            KnowledgeBaseEntry.objects.create(
                account=account,
                title=question,
                content=answer,
                source_type=KnowledgeBaseEntry.SourceType.FAQ,
                origin=origin,
                source_url=source_url[:500],
                is_active=False,
            )
        )
    return created


def import_pairs(account, user, pairs) -> dict:
    """Add pasted or uploaded Q&A for review, and ask AI to draft the missing answers.

    ``{"added", "to_draft", "draft"}``: ``draft`` is the queued ``AIDraft`` (None when every
    question had an answer or AI is off; the owner then writes the answers).
    """
    from apps.ai import api as ai_api
    from apps.ai.models import KnowledgeBaseEntry

    answered = [(q, a) for q, a in pairs if a]
    unanswered = [(q, "") for q, a in pairs if not a]
    created = add_for_review(
        account, answered, origin=KnowledgeBaseEntry.Origin.IMPORT
    ) + add_for_review(account, unanswered, origin=KnowledgeBaseEntry.Origin.AI_DRAFT)
    blank = [e.pk for e in created if not e.content][:MAX_DRAFTED]
    draft = None
    if blank:
        draft = ai_api.request_draft(
            account,
            user,
            "knowledge_answers",
            f"Draft answers to {len(blank)} questions",
            {"entry_ids": blank},
        )
    return {"added": len(created), "to_draft": len(blank), "draft": draft}


# ---- drafting answers -----------------------------------------------------------------------


def _business_knowledge(account, notes: str) -> str:
    from apps.ai import facts as business_facts
    from apps.ai import prompts
    from apps.conversations.api import opening_hours_line

    return "\n".join(
        prompts.knowledge_lines(
            business_name=account.company_name or "",
            business_notes=notes,
            hours_text=opening_hours_line(account),
            facts=business_facts.build(account, business_notes=notes),
        )
    )


def run_answers(draft, provider, notes: str) -> None:
    """Fill the empty answers of the draft's entries from what the business already told AI."""
    from apps.ai.models import KnowledgeBaseEntry

    account = draft.account
    entries = list(
        KnowledgeBaseEntry.objects.filter(
            account=account,
            pk__in=draft.context.get("entry_ids") or [],
            reviewed_at__isnull=True,
            content="",
        )
    )
    if not entries:
        draft.result = {"answered": 0, "unanswered": 0}
        return
    name = account.company_name or "the business"
    system = f"""You help {name} write answers for its knowledge base: the questions its customers \
ask, answered in the business's own voice ("we", "our").

Use ONLY the business knowledge below. If it doesn't answer a question, leave that answer empty: \
never guess prices, dates, policies or promises. Keep each answer short, plain and friendly.

Answer with ONE JSON object and nothing else:
{{"answers": [{{"id": <the question's id>, "answer": "<the answer, or empty>"}}]}}

""" + _business_knowledge(account, notes)
    questions = "\n".join(f"{e.pk}: {e.title}" for e in entries)
    result = provider.chat(
        [ChatMessage("user", "Questions (id: question):\n" + questions)],
        system=system,
        max_tokens=3000,
        temperature=0.2,
    )
    try:
        data = extract_json(result.text)
    except ProposalError as exc:
        from apps.ai.drafting import DraftError

        raise DraftError(str(exc)) from exc
    by_id = {e.pk: e for e in entries}
    answered = 0
    for item in data.get("answers") or []:
        if not isinstance(item, dict):
            continue
        try:
            entry = by_id.get(int(item.get("id") or 0))
        except (TypeError, ValueError):
            continue
        text = str(item.get("answer") or "").strip()[:MAX_ANSWER]
        if entry is not None and text and not entry.content:
            entry.content = text
            entry.save(update_fields=["content", "updated_at"])
            answered += 1
    draft.result = {"answered": answered, "unanswered": len(entries) - answered}
    draft.model = (result.model or "")[:80]


# ---- reading a website ----------------------------------------------------------------------


class WebsiteError(ValueError):
    """The site can't be read; the message is for the owner."""


def normalise_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebsiteError("Enter your website's address, like https://example.com.")
    return urldefrag(url)[0]


def _assert_public(url: str) -> None:
    """Refuse addresses that resolve to private, loopback or otherwise internal networks."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        infos = socket.getaddrinfo(
            host, parts.port or (443 if parts.scheme == "https" else 80)
        )
    except (socket.gaierror, UnicodeError) as exc:
        raise WebsiteError(f"Couldn't find {host}. Check the address.") from exc
    for *_rest, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global or ip.is_multicast:
            raise WebsiteError("That address isn't a public website.")


def _get(url: str) -> tuple[str, str]:
    """``(final url, html)`` for one page. Each redirect is checked like the first address."""
    for _hop in range(MAX_REDIRECTS + 1):
        _assert_public(url)
        try:
            resp = requests.get(
                url,
                timeout=TIMEOUT_SECONDS,
                stream=True,
                allow_redirects=False,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            )
        except requests.RequestException as exc:
            raise WebsiteError("Couldn't reach the website. Try again later.") from exc
        if resp.is_redirect and resp.headers.get("location"):
            url = normalise_url(urljoin(url, resp.headers["location"]))
            resp.close()
            continue
        if resp.status_code >= 400:
            raise WebsiteError(
                f"The website answered with an error ({resp.status_code})."
            )
        if "html" not in resp.headers.get("content-type", "html").lower():
            raise WebsiteError("That address isn't a web page.")
        body = resp.raw.read(MAX_PAGE_BYTES + 1, decode_content=True) or b""
        resp.close()
        return url, body[:MAX_PAGE_BYTES].decode(resp.encoding or "utf-8", "replace")
    raise WebsiteError("The website redirected too many times.")


class _PageText(HTMLParser):
    """Visible text and same-page links. Scripts, styles and navigation chrome are skipped."""

    SKIP = {"script", "style", "noscript", "svg", "template", "head", "nav", "footer"}
    BLOCK = {
        "p",
        "div",
        "li",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
        "section",
    }
    # Kept apart by a space, or "<a>FAQ</a><a>Prices</a>" reads as "FAQPrices".
    INLINE = {"a", "span", "td", "th", "button", "label"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        self._separate(tag)

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        self._separate(tag)

    def _separate(self, tag):
        if tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in self.INLINE:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(ln.split()) for ln in "".join(self.parts).split("\n"))
        return "\n".join(ln for ln in lines if ln)


def page_text(html: str) -> tuple[str, list[str]]:
    parser = _PageText()
    parser.feed(html)
    return parser.text(), parser.links


def read_site(url: str) -> list[tuple[str, str]]:
    """``[(url, text)]`` for the start page and up to ``MAX_PAGES`` - 1 pages it links to on the
    same site, honouring robots.txt. Raises ``WebsiteError``."""
    from urllib.robotparser import RobotFileParser

    start = normalise_url(url)
    host = urlsplit(start).hostname
    robots = RobotFileParser()
    try:
        _url, robots_txt = _get(urljoin(start, "/robots.txt"))
        robots.parse(robots_txt.splitlines())
    except WebsiteError:
        robots.parse([])  # no robots.txt: nothing is disallowed

    pages: list[tuple[str, str]] = []
    queue, seen = [start], {start}
    while queue and len(pages) < MAX_PAGES:
        target = queue.pop(0)
        if not robots.can_fetch(USER_AGENT, target):
            continue
        try:
            final, html = _get(target)
        except WebsiteError:
            if not pages and target == start:
                raise
            continue
        text, links = page_text(html)
        if text:
            pages.append((final, text[:MAX_PAGE_TEXT]))
        for href in links:
            link = urldefrag(urljoin(final, href))[0]
            parts = urlsplit(link)
            if (
                parts.scheme in ("http", "https")
                and parts.hostname == host
                and link not in seen
                and not re.search(
                    r"\.(?:pdf|jpe?g|png|gif|zip|mp4|webp|svg)$", parts.path, re.I
                )
            ):
                seen.add(link)
                queue.append(link)
    if not pages:
        raise WebsiteError("Couldn't find any text on that website.")
    return pages


def run_website(draft, provider) -> None:
    """Read the business's website and add the questions and answers it holds, for review."""
    from apps.ai.drafting import DraftError
    from apps.ai.models import KnowledgeBaseEntry

    account = draft.account
    try:
        pages = read_site(draft.context.get("url") or draft.prompt)
    except WebsiteError as exc:
        raise DraftError(str(exc)) from exc
    budget, chunks = MAX_SITE_TEXT, []
    for url, text in pages:
        if budget <= 0:
            break
        chunks.append(f"=== {url}\n{text[:budget]}")
        budget -= len(text)
    name = account.company_name or "the business"
    system = f"""You turn {name}'s website into a knowledge base: the questions its customers \
ask and the answers the website gives, written in the business's voice ("we", "our").

Use ONLY the page text given. Skip navigation, cookie notices, legal boilerplate and anything a \
customer wouldn't ask about. At most 30 entries; short, plain answers.

Answer with ONE JSON object and nothing else:
{{"entries": [{{"question": "<what a customer would ask>", "answer": "<the answer from the site>", \
"url": "<the page it came from>"}}]}}"""
    result = provider.chat(
        [ChatMessage("user", "\n\n".join(chunks))],
        system=system,
        max_tokens=4000,
        temperature=0.2,
    )
    try:
        data = extract_json(result.text)
    except ProposalError as exc:
        raise DraftError(str(exc)) from exc
    known_urls = {url for url, _ in pages}
    created = 0
    for item in (data.get("entries") or [])[:30]:
        if not isinstance(item, dict):
            continue
        pairs = _tidy(
            [(str(item.get("question") or ""), str(item.get("answer") or ""))]
        )
        if not pairs or not pairs[0][1]:
            continue
        url = str(item.get("url") or "")
        created += len(
            add_for_review(
                account,
                pairs,
                origin=KnowledgeBaseEntry.Origin.WEBSITE,
                source_url=url if url in known_urls else pages[0][0],
            )
        )
    draft.result = {"added": created, "pages": len(pages)}
    draft.model = (result.model or "")[:80]
