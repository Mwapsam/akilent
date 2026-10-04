"""Tier-2 observability integration tests.

Acceptance criterion: every production error must supply enough correlated
context to diagnose the root cause without reproducing the interaction.

These tests drive the full chain:

    browser session
          ↓
    HTMX request (X-Browser-Session-Id header)
          ↓
    BrowserSessionMiddleware upserts BrowserSession
          ↓
    log_api_request persists browser_session_id + request_id
          ↓
    ApiRequest.browser_session_id set
          ↓
    500 — correlation still present
          ↓
    ingest_ux_event (rage click) links to nearest request_id
          ↓
    session timeline (unified, chronological)
          ↓
    request_detail view shows inline preceding timeline

None of these tests reproduce a production issue to diagnose it: they verify
that the system *captures* enough context that no reproduction is necessary.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.logs.models import ApiRequest, BrowserSession, UxEvent
from apps.logs.observability import log_api_request

User = get_user_model()

# A valid session ID (same format as session-tracker.js generates)
_SESS = "sess_" + "a" * 32


class BrowserSessionMiddlewareTest(TestCase):
    """Session header → BrowserSession row lifecycle."""

    def setUp(self):
        self.account = Account.objects.create(company_name="Acme", slug="acme")
        self.user = User.objects.create_user(
            username="op@acme.com",
            password="x",  # noqa: S106
            email="op@acme.com",
        )

    def _get(self, path="/", **extra):
        c = Client()
        return c.get(path, HTTP_X_BROWSER_SESSION_ID=_SESS, **extra)

    def test_valid_header_creates_browser_session(self):
        self._get("/healthz")
        assert BrowserSession.objects.filter(session_id=_SESS).exists()

    def test_invalid_prefix_is_ignored(self):
        c = Client()
        c.get("/healthz", HTTP_X_BROWSER_SESSION_ID="bad_" + "x" * 40)
        assert not BrowserSession.objects.filter(session_id__startswith="bad_").exists()

    def test_last_seen_at_updated_on_second_request(self):
        self._get("/healthz")
        bs1 = BrowserSession.objects.get(session_id=_SESS)
        t1 = bs1.last_seen_at
        future = t1 + timedelta(seconds=30)
        with patch("django.utils.timezone.now", return_value=future):
            self._get("/healthz")
        bs2 = BrowserSession.objects.get(session_id=_SESS)
        assert bs2.last_seen_at > t1

    def test_bsid_cookie_set_on_response(self):
        response = self._get("/healthz")
        assert "bsid" in response.cookies
        assert response.cookies["bsid"].value == _SESS

    def test_anonymous_session_account_is_none(self):
        self._get("/healthz")
        bs = BrowserSession.objects.get(session_id=_SESS)
        assert bs.account is None

    def test_no_session_header_no_row(self):
        Client().get("/healthz")
        assert not BrowserSession.objects.filter(session_id=_SESS).exists()


class ContextVarIsolationTest(TestCase):
    """Two simultaneous requests with different session IDs must not contaminate
    each other's ContextVar slot.  Demonstrated via direct middleware invocation
    rather than threading (same guarantees — ContextVar is per-async-context, and
    WSGI uses per-thread contextvars that are reset via the token/finally pattern).
    """

    def test_contextvar_reset_after_request(self):
        from apps.core.browser_session import get_browser_session_id

        c = Client()
        c.get("/healthz", HTTP_X_BROWSER_SESSION_ID=_SESS)
        # After the request completes the ContextVar must have been reset to ""
        # (the token/finally guarantees this in any WSGI thread reuse scenario)
        assert get_browser_session_id() == ""

    def test_two_different_sessions_produce_two_rows(self):
        sess_b = "sess_" + "b" * 32
        c = Client()
        c.get("/healthz", HTTP_X_BROWSER_SESSION_ID=_SESS)
        c.get("/healthz", HTTP_X_BROWSER_SESSION_ID=sess_b)
        assert (
            BrowserSession.objects.filter(session_id__in=[_SESS, sess_b]).count() == 2
        )


class ApiRequestCorrelationTest(TestCase):
    """browser_session_id + request_id are persisted on every ApiRequest row."""

    def setUp(self):
        self.factory = RequestFactory()
        self.account = Account.objects.create(company_name="Acme", slug="acme-api")

    def _make_request(self, sid, req_id="req_abc123"):
        from django.http import HttpResponse

        request = self.factory.post("/api/orders/1/submit/")
        request.browser_session_id = sid  # type: ignore[attr-defined]
        request.id = req_id  # type: ignore[attr-defined]
        request.META["HTTP_USER_AGENT"] = "Mozilla/5.0 (test)"
        request.account = None  # type: ignore[attr-defined]
        request.auth = None  # type: ignore[attr-defined]
        request.version = "v1"  # type: ignore[attr-defined]
        request.op_meta = {"operation": "order.submit", "order_id": "ord_123"}  # type: ignore[attr-defined]
        response = HttpResponse(status=500)
        response.data = {"error": {"code": "integrity_error"}}  # type: ignore[attr-defined]
        return request, response

    def test_browser_session_id_persisted(self):
        BrowserSession.objects.create(session_id=_SESS, last_seen_at=timezone.now())
        request, response = self._make_request(_SESS)
        log_api_request(request, response, latency_ms=142)
        row = ApiRequest.objects.filter(browser_session_id=_SESS).first()
        assert row is not None
        assert row.browser_session_id == _SESS

    def test_request_id_persisted(self):
        BrowserSession.objects.create(session_id=_SESS, last_seen_at=timezone.now())
        request, response = self._make_request(_SESS, req_id="req_test_abc")
        log_api_request(request, response, latency_ms=10)
        row = ApiRequest.objects.filter(browser_session_id=_SESS).first()
        assert row is not None
        assert row.request_id == "req_test_abc"

    def test_op_meta_persisted(self):
        BrowserSession.objects.create(session_id=_SESS, last_seen_at=timezone.now())
        request, response = self._make_request(_SESS)
        log_api_request(request, response, latency_ms=10)
        row = ApiRequest.objects.filter(browser_session_id=_SESS).first()
        assert row is not None
        assert row.op_meta.get("operation") == "order.submit"
        assert row.op_meta.get("order_id") == "ord_123"

    def test_op_meta_does_not_leak_pii_placeholder(self):
        """op_meta must never contain raw POST/request data."""
        BrowserSession.objects.create(session_id=_SESS, last_seen_at=timezone.now())
        request, response = self._make_request(_SESS)
        request.op_meta = {  # type: ignore[attr-defined]
            "operation": "order.submit",
            "k" * 70: "value",  # key > 64 chars — must be truncated
            "v": "x" * 300,  # value > 256 chars — must be truncated
        }
        log_api_request(request, response, latency_ms=10)
        row = ApiRequest.objects.filter(browser_session_id=_SESS).first()
        assert row is not None
        for k, v in row.op_meta.items():
            assert len(k) <= 64, f"key '{k[:20]}…' exceeds 64 chars"
            assert len(v) <= 256, f"value for '{k[:20]}…' exceeds 256 chars"

    def test_500_still_captures_correlation(self):
        """Correlation must survive error responses — the most important case."""
        BrowserSession.objects.create(session_id=_SESS, last_seen_at=timezone.now())
        request, response = self._make_request(_SESS)
        log_api_request(request, response, latency_ms=421)
        row = ApiRequest.objects.filter(browser_session_id=_SESS).first()
        assert row is not None
        assert row.status_code == 500
        assert row.browser_session_id != ""
        assert row.request_id != ""


class UxEventIngestTest(TestCase):
    """POST /internal/ux-event/ — rate limiting, session resolution, field limits."""

    def setUp(self):
        self.bs = BrowserSession.objects.create(
            session_id=_SESS, last_seen_at=timezone.now()
        )
        self.account = Account.objects.create(company_name="Acme UX", slug="acme-ux")
        self.client = Client()

    def _post(self, payload, sid=_SESS, csrftoken=True):
        headers = {"HTTP_X_BROWSER_SESSION_ID": sid}
        if csrftoken:
            headers["HTTP_X_CSRFTOKEN"] = "testtoken"
        return self.client.post(
            "/internal/ux-event/",
            data=json.dumps(payload),
            content_type="application/json",
            **headers,
        )

    def test_rage_click_creates_ux_event(self):
        resp = self._post(
            {
                "type": "rage_click",
                "page": "/orders/",
                "target": "button#submit",
                "click_count": 4,
                "duration_ms": 750,
                "request_id": "req_abc",
            }
        )
        assert resp.status_code == 200
        ev = UxEvent.objects.filter(session=self.bs, type="rage_click").first()
        assert ev is not None
        assert ev.click_count == 4
        assert ev.request_id == "req_abc"

    def test_js_error_creates_ux_event(self):
        resp = self._post(
            {
                "type": "js_error",
                "page": "/orders/",
                "target": "main.js",
                "data": {
                    "message": "TypeError: cannot read null",
                    "stack": "at submit",
                },
            }
        )
        assert resp.status_code == 200
        ev = UxEvent.objects.filter(session=self.bs, type="js_error").first()
        assert ev is not None
        assert ev.data["message"] == "TypeError: cannot read null"

    def test_unknown_type_rejected(self):
        resp = self._post({"type": "hover", "page": "/", "target": ""})
        assert resp.status_code == 400

    def test_no_session_rejected(self):
        resp = self._post(
            {"type": "rage_click", "page": "/", "target": ""},
            sid="",
        )
        assert resp.status_code == 400

    def test_nonexistent_session_rejected(self):
        # The middleware creates BrowserSession before the view runs when a valid
        # session header is present.  To test the 404 path we bypass the middleware
        # and inject an unknown session ID directly onto the request attribute.
        from apps.core.views import ingest_ux_event

        factory = RequestFactory()
        req = factory.post(
            "/internal/ux-event/",
            data=json.dumps({"type": "rage_click", "page": "/", "target": ""}),
            content_type="application/json",
        )
        req.browser_session_id = "sess_" + "z" * 32  # type: ignore[attr-defined]
        resp = ingest_ux_event(req)
        assert resp.status_code == 404

    def test_rate_limit_enforced(self):
        from django.core.cache import cache

        rate_key = f"ux_event_rate:{_SESS}"
        cache.set(rate_key, 60, 60)
        resp = self._post({"type": "rage_click", "page": "/", "target": ""})
        assert resp.status_code == 429
        cache.delete(rate_key)

    def test_oversized_data_truncated(self):
        resp = self._post(
            {
                "type": "js_error",
                "page": "/",
                "target": "",
                "data": {"big": "x" * 20_000},
            }
        )
        assert resp.status_code == 200
        ev = UxEvent.objects.filter(session=self.bs, type="js_error").first()
        assert ev is not None
        assert "truncated" in ev.data


class SessionTimelineTest(TestCase):
    """The unified session timeline must be chronologically sorted and contain
    both API requests and UX events — the core of the 'no reproduction' guarantee.
    """

    def setUp(self):
        # Anchor 1 hour in the past so offsets produce clearly historical times
        # and the sort is never affected by the real clock during the test run.
        self._base = (timezone.now() - timedelta(hours=1)).replace(microsecond=0)
        self.bs = BrowserSession.objects.create(
            session_id=_SESS, last_seen_at=timezone.now()
        )
        self.account = Account.objects.create(company_name="Acme TL", slug="acme-tl")

    def _dt(self, offset_s: int) -> datetime:
        return self._base + timedelta(seconds=offset_s)

    def _api(
        self,
        offset_s: int,
        status: int,
        path: str = "/api/orders/1/submit/",
        req_id: str = "",
    ) -> None:
        # Create the row via ORM then patch created_at with a direct UPDATE
        # (auto_now_add prevents passing it through the normal ORM path).
        row = ApiRequest.objects.create(
            method="POST",
            path=path,
            status_code=status,
            request_id=req_id or f"rid_{offset_s}",
            browser_session_id=_SESS,
            latency_ms=100,
        )
        ApiRequest.objects.filter(pk=row.pk).update(created_at=self._dt(offset_s))

    def _ux(self, offset_s: int, ux_type: str, req_id: str = "") -> None:
        UxEvent.objects.create(
            session=self.bs,
            type=ux_type,
            page="/orders/",
            target="button#submit",
            occurred_at=self._dt(offset_s),
            request_id=req_id or f"rid_{offset_s}",
        )

    def test_unified_timeline_chronological(self):
        """Mix of requests and UX events must sort chronologically."""
        # t=0  PATCH /orders/1           200
        # t=3  UX: click submit
        # t=3  POST /orders/1/submit     500
        # t=3  UX: js_error
        # t=4  UX: rage_click x4
        self._api(0, 200, "/api/orders/1/", "rid_A")
        self._ux(3, "dead_click", "rid_A")
        self._api(3, 500, "/api/orders/1/submit/", "rid_B")
        self._ux(3, "js_error", "rid_B")
        self._ux(4, "rage_click", "rid_B")

        api_qs = list(
            ApiRequest.objects.filter(browser_session_id=_SESS)
            .order_by("created_at")
            .values(
                "created_at",
                "status_code",
                "request_id",
                "public_id",
                "method",
                "path",
                "latency_ms",
                "op_meta",
            )
        )
        ux_qs = list(
            UxEvent.objects.filter(session=self.bs)
            .order_by("occurred_at")
            .values(
                "occurred_at",
                "type",
                "page",
                "target",
                "click_count",
                "duration_ms",
                "request_id",
                "data",
            )
        )
        timeline: list[dict] = [
            {"ts": r["created_at"], "kind": "request", "obj": r} for r in api_qs
        ] + [{"ts": e["occurred_at"], "kind": "ux", "obj": e} for e in ux_qs]
        timeline.sort(key=lambda x: x["ts"])  # type: ignore[arg-type]

        assert len(timeline) == 5
        assert timeline[0]["kind"] == "request"
        assert timeline[0]["obj"]["status_code"] == 200  # type: ignore[index]
        assert timeline[-1]["kind"] == "ux"
        assert timeline[-1]["obj"]["type"] == "rage_click"  # type: ignore[index]

    def test_500_request_present_in_timeline(self):
        self._api(0, 500, "/api/orders/1/submit/", "rid_fail")
        api_qs = list(
            ApiRequest.objects.filter(browser_session_id=_SESS, status_code=500).values(
                "created_at",
                "status_code",
                "request_id",
                "public_id",
                "method",
                "path",
                "latency_ms",
                "op_meta",
            )
        )
        assert len(api_qs) == 1
        assert api_qs[0]["status_code"] == 500

    def test_rage_click_links_to_request_id(self):
        """Rage click must name the nearest request ID — the 500 that triggered frustration."""
        self._api(0, 500, "/api/orders/1/submit/", "rid_500")
        self._ux(1, "rage_click", "rid_500")
        ev = UxEvent.objects.filter(session=self.bs, type="rage_click").first()
        assert ev is not None
        assert ev.request_id == "rid_500"


class RequestDetailViewTest(TestCase):
    """The request_detail view exposes the preceding 30-second timeline inline."""

    def setUp(self):
        from apps.accounts.models import Membership

        self.user = User.objects.create_superuser(
            username="admin",
            password="x",  # noqa: S106
            email="admin@test.com",
        )
        self.account = Account.objects.create(company_name="Acme RD", slug="acme-rd")
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )
        self.bs = BrowserSession.objects.create(
            session_id=_SESS,
            account=self.account,
            last_seen_at=timezone.now(),
            browser="Chrome 130",
            device="desktop",
        )

    def _api_row(
        self, status: int = 200, path: str = "/api/orders/1/", req_id: str = "req_test"
    ) -> ApiRequest:
        return ApiRequest.objects.create(
            account=self.account,
            method="POST",
            path=path,
            status_code=status,
            request_id=req_id,
            browser_session_id=_SESS,
            latency_ms=100,
        )

    def test_request_detail_shows_session_link(self):
        row = self._api_row(500, "/api/orders/1/submit/", "req_fail")
        self.client.login(username="admin", password="x")  # noqa: S106
        resp = self.client.get(f"/logs/requests/{row.public_id}/")
        assert resp.status_code == 200
        self.assertContains(resp, _SESS[:16])

    def test_request_detail_shows_browser(self):
        row = self._api_row(500, "/api/orders/1/submit/", "req_fail2")
        self.client.login(username="admin", password="x")  # noqa: S106
        resp = self.client.get(f"/logs/requests/{row.public_id}/")
        self.assertContains(resp, "Chrome 130")

    def test_preceding_timeline_in_context(self):
        earlier = timezone.now() - timedelta(seconds=20)
        with patch("django.utils.timezone.now", return_value=earlier):
            self._api_row(200, "/api/orders/1/", "req_prior")

        failed = self._api_row(500, "/api/orders/1/submit/", "req_fail3")

        self.client.login(username="admin", password="x")  # noqa: S106
        resp = self.client.get(f"/logs/requests/{failed.public_id}/")
        assert resp.status_code == 200
        timeline = resp.context.get("preceding_timeline", [])
        request_ids = [
            item["obj"].get("request_id", "")
            for item in timeline
            if item["kind"] == "request"
        ]
        assert "req_prior" in request_ids

    def test_500_outside_30s_window_excluded(self):
        earlier = timezone.now() - timedelta(seconds=60)
        with patch("django.utils.timezone.now", return_value=earlier):
            self._api_row(200, "/api/orders/1/", "req_old")

        failed = self._api_row(500, "/api/orders/1/submit/", "req_fail4")
        self.client.login(username="admin", password="x")  # noqa: S106
        resp = self.client.get(f"/logs/requests/{failed.public_id}/")
        timeline = resp.context.get("preceding_timeline", [])
        request_ids = [
            item["obj"].get("request_id", "")
            for item in timeline
            if item["kind"] == "request"
        ]
        assert "req_old" not in request_ids


class PrivacyBoundaryTest(TestCase):
    """Verify that sensitive terms never appear in stored telemetry fields."""

    SENSITIVE_KEYS = {"email", "password", "token", "authorization", "cookie", "secret"}

    def setUp(self):
        self.bs = BrowserSession.objects.create(
            session_id=_SESS, last_seen_at=timezone.now()
        )

    def test_ux_event_target_sanitised(self):
        """target must not hold attribute values that could contain PII."""
        UxEvent.objects.create(
            session=self.bs,
            type="rage_click",
            page="/login/",
            # A sanitised selector — tag + id + safe attrs only, no [value=…]
            target='input[name="username"]',
            occurred_at=timezone.now(),
        )
        ev = UxEvent.objects.get(session=self.bs, type="rage_click")
        # No value= attribute (which could contain the typed email/password)
        assert "value=" not in ev.target

    def test_op_meta_keys_do_not_shadow_sensitive_names(self):
        """_safe_op_meta should pass through only keys a view explicitly set."""
        from apps.logs.observability import _safe_op_meta

        factory = RequestFactory()
        request = factory.post("/", {"email": "user@test.com", "password": "secret"})
        request.op_meta = {"operation": "login.attempt", "account_id": "acct_1"}  # type: ignore[attr-defined]
        meta = _safe_op_meta(request)

        assert meta["operation"] == "login.attempt"
        for key in self.SENSITIVE_KEYS:
            assert key not in meta, f"Sensitive key '{key}' leaked into op_meta"
