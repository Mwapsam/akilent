"""In-place navigation: base.html's two render modes and ShellMiddleware's fallbacks.

The rule under test: a boosted navigation gets a fragment only when the fragment can safely
replace <main> in the shell the browser already has; every other case becomes an ordinary page
load (HX-Redirect / HX-Refresh), which is exactly what the app did before.
"""
import pytest
from django.contrib.auth.models import User
from django.http import HttpResponse, HttpResponseRedirect
from django.template import Context, Template
from django.test import RequestFactory

from apps.accounts.models import Account, Membership
from apps.core.htmx import ShellMiddleware, full_page_load, is_background, is_shell_swap

BOOSTED = {"HTTP_HX_REQUEST": "true", "HTTP_HX_BOOSTED": "true", "HTTP_X_AKILENT_SHELL": "app"}


@pytest.fixture
def owner(db):
    user = User.objects.create_user("owner", "owner@example.com", "Sup3r-secret-pw")
    account = Account.objects.create(
        company_name="Acme", selected_services=Account.Services.EMAIL,
        onboarding_state=Account.Onboarding.ACCOUNT_CREATED, email_verified=True,
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user


@pytest.fixture
def owner_client(client, owner):
    client.force_login(owner)
    return client


# --- base.html render modes -----------------------------------------------------------------

def test_full_load_renders_the_whole_shell(owner_client):
    resp = owner_client.get("/dashboard/")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert '<meta name="akilent-shell" content="app">' in html
    assert 'hx-boost="true"' in html
    assert '<main id="main"' in html and "hx-history-elt" in html
    assert "HX-Boosted" in resp["Vary"]


def test_boosted_navigation_gets_only_the_page(owner_client):
    resp = owner_client.get("/dashboard/", **BOOSTED)
    html = resp.content.decode()
    assert resp.status_code == 200
    assert not html.lstrip().lower().startswith("<!doctype")
    assert html.lstrip().startswith("<title>")
    assert '<meta name="akilent-shell" content="app">' in html
    # No shell chrome, but the nav and breadcrumbs come along out of band.
    for shell_only in ("<aside", 'aria-label="Account menu"', "app-shell-pl", '<main id="main"', "toggleSidebar"):
        assert shell_only not in html, shell_only
    assert 'id="shell-nav-desktop" hx-swap-oob="innerHTML"' in html
    assert 'id="shell-nav-mobile" hx-swap-oob="innerHTML"' in html
    assert 'id="shell-crumbs" hx-swap-oob="innerHTML"' in html
    assert resp["HX-Push-Url"] == "/dashboard/"


def test_fragment_nav_marks_the_new_page_active(owner_client):
    html = owner_client.get("/contacts/", **BOOSTED).content.decode()
    customers = html.split('href="/contacts/"', 1)[1].split(">", 1)[0]
    assert 'aria-current="page"' in customers


def test_boosted_request_is_a_page_not_a_background_partial(owner_client):
    """contacts_list answers plain HX requests with just the results; a navigation needs the page."""
    background = owner_client.get("/contacts/", HTTP_HX_REQUEST="true").content.decode()
    boosted = owner_client.get("/contacts/", **BOOSTED).content.decode()
    assert "<title>" not in background
    assert "<title>" in boosted


def test_other_shell_gets_a_real_page_load(owner_client):
    resp = owner_client.get("/dashboard/", **{**BOOSTED, "HTTP_X_AKILENT_SHELL": "operator"})
    assert resp.status_code == 204
    assert resp["HX-Redirect"] == "/dashboard/"


def test_boosted_request_without_a_shell_gets_a_real_page_load(owner_client):
    headers = {k: v for k, v in BOOSTED.items() if k != "HTTP_X_AKILENT_SHELL"}
    resp = owner_client.get("/dashboard/", **headers)
    assert resp["HX-Redirect"] == "/dashboard/"


def test_page_outside_the_shell_gets_a_real_page_load(owner_client):
    resp = owner_client.get("/", **BOOSTED)  # the landing page is its own document
    assert resp["HX-Redirect"] == "/"


def test_history_restore_gets_the_full_page(owner_client):
    resp = owner_client.get("/dashboard/", HTTP_HX_REQUEST="true", HTTP_HX_HISTORY_RESTORE_REQUEST="true")
    assert resp.content.decode().lstrip().lower().startswith("<!doctype")
    assert not resp.has_header("HX-Redirect")


def test_signed_out_pages_are_not_boosted(client, db):
    html = client.get("/auth/login/").content.decode()
    assert 'hx-boost="true"' not in html


# --- ShellMiddleware fallbacks, in isolation ---------------------------------------------------

def _run(response, *, method="get", path="/somewhere/", **headers):
    request = getattr(RequestFactory(), method)(path, **{**BOOSTED, **headers})
    return ShellMiddleware(lambda r: response)(request)


def _fragment(body=""):
    return HttpResponse('<title>T</title>\n<meta name="akilent-shell" content="app">\n' + body)


def test_redirect_to_another_site_leaves_the_app():
    resp = _run(HttpResponseRedirect("https://checkout.flutterwave.com/pay/abc"))
    assert resp["HX-Redirect"] == "https://checkout.flutterwave.com/pay/abc"


def test_same_site_redirect_is_left_for_htmx_to_follow():
    resp = _run(HttpResponseRedirect("/contacts/"), method="post")
    assert resp.status_code == 302 and not resp.has_header("HX-Redirect")


def test_download_is_fetched_for_real():
    response = HttpResponse("a,b", content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="x.csv"'
    assert _run(response)["HX-Redirect"] == "/somewhere/"


def test_page_scripts_force_a_full_load_unless_marked_safe():
    assert _run(_fragment("<script src='/static/js/editor.js'></script>"))["HX-Redirect"] == "/somewhere/"
    ok = _run(_fragment('<script type="application/json">[]</script><script data-shell-safe>1</script>'))
    assert ok.status_code == 200 and ok["HX-Push-Url"] == "/somewhere/"


def test_a_post_that_needs_a_full_page_refreshes_instead_of_repeating():
    resp = _run(HttpResponse("<!DOCTYPE html><html></html>"), method="post")
    assert resp["HX-Refresh"] == "true"


def test_a_post_that_re_renders_the_form_is_not_pushed():
    resp = _run(_fragment("<form></form>"), method="post")
    assert resp.status_code == 200 and not resp.has_header("HX-Push-Url")


def test_non_boosted_requests_are_untouched():
    request = RequestFactory().get("/x/", HTTP_HX_REQUEST="true")
    response = ShellMiddleware(lambda r: HttpResponse("<tr></tr>"))(request)
    assert response.content == b"<tr></tr>" and not response.has_header("HX-Redirect")
    assert is_background(request) and not is_shell_swap(request)


def test_full_page_load_decorator():
    view = full_page_load(lambda request: HttpResponse("editor"))
    boosted = RequestFactory().get("/editor/?id=1", **BOOSTED)
    assert view(boosted)["HX-Redirect"] == "/editor/?id=1"
    assert view(RequestFactory().get("/editor/")).content == b"editor"


# --- nav helpers ----------------------------------------------------------------------------

def test_nav_targets_resolves_names_and_paths():
    out = Template('{% load nav %}{% nav_targets "inbox email/campaigns /x/" %}').render(Context({}))
    assert out.split() == ["/inbox/", "/email/campaigns/", "/x/"]
