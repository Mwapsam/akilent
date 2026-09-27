"""apps.accounts.utils.hx_toast: the HX-Trigger toast helper for in-place mutations.

An hx-post that swaps a fragment in place has no full page load to carry a Django flash
message, so a confirmation ("Saved", "Tag removed") has to ride the response itself. HTMX
turns HX-Trigger into a same-named DOM event; static/js/app.js listens for "toast" and hands
it to the same store a flash message renders into.
"""
import json

from django.http import HttpResponse

from apps.accounts.utils import hx_toast


def test_sets_the_hx_trigger_header():
    response = hx_toast(HttpResponse(), "success", "Saved.")
    payload = json.loads(response["HX-Trigger"])
    assert payload == {"toast": {"type": "success", "message": "Saved."}}


def test_merges_into_an_existing_hx_trigger_rather_than_overwriting_it():
    response = HttpResponse()
    response["HX-Trigger"] = json.dumps({"refreshList": True})
    hx_toast(response, "danger", "Could not remove tag.")
    payload = json.loads(response["HX-Trigger"])
    assert payload["refreshList"] is True
    assert payload["toast"] == {"type": "danger", "message": "Could not remove tag."}


def test_returns_the_response_for_chaining():
    response = HttpResponse()
    assert hx_toast(response, "info", "Hi") is response
