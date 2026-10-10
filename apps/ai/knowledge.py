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

import codecs
import csv
import io
import ipaddress
import logging
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import requests
from django.utils import timezone
from requests.adapters import HTTPAdapter

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
TIMEOUT_SECONDS = 8  # to connect, and between bytes
PAGE_DEADLINE_SECONDS = 20  # for a whole page, however slowly it trickles in
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
    from apps.ai import knowledge_write

    return knowledge_write.bulk_create_for_review(
        account, pairs, origin=origin, source_url=source_url
    )


def import_pairs(account, user, pairs) -> dict:
    """Add pasted or uploaded Q&A for review, and ask AI to draft the missing answers.

    ``{"added", "to_draft", "to_write", "draft"}``: ``draft`` is the queued ``AIDraft`` for the
    first ``MAX_DRAFTED`` unanswered questions (None when every question had an answer or AI is
    off); ``to_write`` counts the unanswered questions left for the owner.
    """
    from apps.ai import api as ai_api
    from apps.ai.models import KnowledgeBaseEntry

    created = add_for_review(account, pairs, origin=KnowledgeBaseEntry.Origin.IMPORT)
    blank = [e.pk for e in created if not e.content]
    queued = blank[:MAX_DRAFTED]
    draft = None
    if queued:
        draft = ai_api.request_draft(
            account,
            user,
            "knowledge_answers",
            f"Draft answers to {len(queued)} questions",
            {"entry_ids": queued},
        )
    if draft is None:
        queued = []
    else:
        # Labelled "Drafted by AI" only when AI really is drafting them.
        from apps.ai import knowledge_write

        qs = KnowledgeBaseEntry.objects.filter(pk__in=queued)
        knowledge_write.bulk_update_entries(
            account, qs, origin=KnowledgeBaseEntry.Origin.AI_DRAFT
        )
    return {
        "added": len(created),
        "to_draft": len(queued),
        "to_write": len(blank) - len(queued),
        "draft": draft,
    }


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
    ids = {e.pk for e in entries}
    updates: list[tuple[int, str]] = []
    for item in data.get("answers") or []:
        if not isinstance(item, dict):
            continue
        try:
            pk = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        text = str(item.get("answer") or "").strip()[:MAX_ANSWER]
        if pk not in ids or not text:
            continue
        updates.append((pk, text))

    answered = 0
    if updates:
        from django.db import transaction

        from apps.ai import knowledge_write

        now = timezone.now()
        with transaction.atomic():
            for pk, text in updates:
                answered += KnowledgeBaseEntry.objects.filter(
                    pk=pk, content="", reviewed_at__isnull=True
                ).update(content=text, updated_at=now)
            if answered:
                knowledge_write.bump_version(account.pk)
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


def _public_ip(url: str) -> str:
    """The address to connect to for ``url``, after checking every address its host resolves to
    is public. Refuses private, loopback, link-local and otherwise internal networks."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        infos = socket.getaddrinfo(
            host, parts.port or (443 if parts.scheme == "https" else 80)
        )
    except (socket.gaierror, UnicodeError) as exc:
        raise WebsiteError(f"Couldn't find {host}. Check the address.") from exc
    addresses = []
    for *_rest, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global or ip.is_multicast:
            raise WebsiteError("That address isn't a public website.")
        addresses.append(str(ip))
    if not addresses:
        raise WebsiteError(f"Couldn't find {host}. Check the address.")
    return addresses[0]


class _PinnedTLS(HTTPAdapter):
    """Connect to an IP address while checking the certificate (and sending SNI) for the
    host name, so the address that was checked is the one that is used."""

    def __init__(self, host: str, **kwargs):
        self._host = host
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["server_hostname"] = self._host
        kwargs["assert_hostname"] = self._host
        super().init_poolmanager(*args, **kwargs)


def _open(url: str, *, ip: str, accept: str):
    """A streamed response for ``url`` fetched from ``ip`` (already checked by ``_public_ip``).

    Never resolves the host again, so a DNS answer that changes between the check and the
    connection (DNS rebinding) can't send the request inside the network. Environment proxies
    are ignored for the same reason.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"[{ip}]" if ":" in ip else ip
    if parts.port:
        netloc += f":{parts.port}"
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", _PinnedTLS(host))
    try:
        resp = session.get(
            urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, "")),
            timeout=TIMEOUT_SECONDS,
            stream=True,
            allow_redirects=False,
            headers={
                "Host": parts.netloc.rsplit("@", 1)[-1],
                "User-Agent": USER_AGENT,
                "Accept": accept,
            },
        )
    except BaseException:
        session.close()
        raise
    # The body is still streaming: the session (and its pooled connection) goes with the response.
    close_response = resp.close

    def close():
        close_response()
        session.close()

    resp.close = close  # type: ignore[method-assign]
    return resp


def _read_body(resp) -> bytes:
    """At most ``MAX_PAGE_BYTES``, within ``PAGE_DEADLINE_SECONDS`` overall."""
    deadline = time.monotonic() + PAGE_DEADLINE_SECONDS
    chunks, size = [], 0
    for chunk in resp.iter_content(64 * 1024):
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_PAGE_BYTES:
            break
        if time.monotonic() > deadline:
            raise WebsiteError("The website is too slow to read. Try again later.")
    return b"".join(chunks)[:MAX_PAGE_BYTES]


_HEADER_CHARSET = re.compile(r"charset=[\"']?([\w.:-]+)", re.I)
_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?([\w.:-]+)", re.I)


def _decode(body: bytes, content_type: str) -> str:
    """The page as text. Charset from the header, else the page's own <meta>, else UTF-8 (not
    requests' ISO-8859-1 default, which garbles "€20" into "â‚¬20")."""
    found = _HEADER_CHARSET.search(content_type or "")
    name = found.group(1) if found else ""
    if not name:
        meta = _META_CHARSET.search(body[:4096])
        name = meta.group(1).decode("ascii", "ignore") if meta else ""
    try:
        codecs.lookup(name or "utf-8")
    except LookupError:
        name = "utf-8"
    return body.decode(name or "utf-8", "replace")


def _get(url: str, *, html: bool = True) -> tuple[str, str]:
    """``(final url, text)`` for one page (``html=False`` for robots.txt, served as plain text).
    Each redirect is checked like the first address."""
    accept = "text/html" if html else "text/plain"
    for _hop in range(MAX_REDIRECTS + 1):
        ip = _public_ip(url)
        try:
            resp = _open(url, ip=ip, accept=accept)
        except requests.RequestException as exc:
            raise WebsiteError("Couldn't reach the website. Try again later.") from exc
        try:
            if resp.is_redirect and resp.headers.get("location"):
                url = normalise_url(urljoin(url, resp.headers["location"]))
                continue
            if resp.status_code >= 400:
                raise WebsiteError(
                    f"The website answered with an error ({resp.status_code})."
                )
            content_type = resp.headers.get("content-type", "")
            if html and content_type and "html" not in content_type.lower():
                raise WebsiteError("That address isn't a web page.")
            try:
                body = _read_body(resp)
            except requests.RequestException as exc:
                raise WebsiteError(
                    "Couldn't read the website. Try again later."
                ) from exc
            return url, _decode(body, content_type)
        finally:
            resp.close()
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


def _robots(page_url: str):
    """The site's robots.txt rules (served as plain text); none when it has no robots.txt."""
    from urllib.robotparser import RobotFileParser

    robots = RobotFileParser()
    try:
        _url, text = _get(urljoin(page_url, "/robots.txt"), html=False)
        robots.parse(text.splitlines())
    except WebsiteError:
        robots.parse([])
    return robots


def read_site(url: str) -> list[tuple[str, str]]:
    """``[(url, text)]`` for the start page and up to ``MAX_PAGES`` - 1 pages it links to on the
    same site, honouring robots.txt. Raises ``WebsiteError``."""
    start = normalise_url(url)
    robots = _robots(start)
    hosts = {urlsplit(start).hostname}

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
        if target == start:
            # example.com often redirects to www.example.com: its links are on that host, and
            # its robots.txt is the one that applies.
            final_host = urlsplit(final).hostname
            if final_host not in hosts:
                hosts.add(final_host)
                robots = _robots(final)
        text, links = page_text(html)
        if text:
            pages.append((final, text[:MAX_PAGE_TEXT]))
        for href in links:
            link = urldefrag(urljoin(final, href))[0]
            parts = urlsplit(link)
            if (
                parts.scheme in ("http", "https")
                and parts.hostname in hosts
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
    pages = [tuple(p) for p in draft.context.get("pages") or []]
    if not pages:
        try:
            pages = read_site(draft.context.get("url") or draft.prompt)
        except WebsiteError as exc:
            raise DraftError(str(exc)) from exc
        # Kept until the model has answered: a retry after a model timeout reuses these instead
        # of reading the whole site again.
        draft.context = {**draft.context, "pages": [list(p) for p in pages]}
        draft.save(update_fields=["context"])
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
    draft.context = {k: v for k, v in draft.context.items() if k != "pages"}
    draft.save(update_fields=["context"])
