"""apps.accounts.utils.hx_toast: the shared X-Toast helper for in-place mutations.

An hx-post that swaps a fragment in place has no full page load to carry a Django flash
message, so a confirmation ("Saved", "Tag removed") has to ride the response itself.
static/js/app.js already reads X-Toast on every htmx:afterRequest (background polls and
boosted navigations alike); this is that same wire format, not a second one — apps/email/views.py
used to carry its own private copy of exactly this function before it moved here.
"""
from urllib.parse import unquote

from django.http import HttpResponse

from apps.accounts.utils import hx_toast


def test_sets_the_x_toast_header_in_the_existing_wire_format():
    response = hx_toast(HttpResponse(), "success", "Saved.")
    kind, _, message = response["X-Toast"].partition("|")
    assert kind == "success"
    assert unquote(message) == "Saved."


def test_url_encodes_the_message():
    response = hx_toast(HttpResponse(), "danger", "Could not remove tag & retry?")
    _, _, message = response["X-Toast"].partition("|")
    assert unquote(message) == "Could not remove tag & retry?"


def test_returns_the_response_for_chaining():
    response = HttpResponse()
    assert hx_toast(response, "info", "Hi") is response
