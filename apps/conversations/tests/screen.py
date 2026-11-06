"""What a conversation shows on screen, for tests that read it.

The details panel loads on its own once the conversation shows (views.conversation_panel), so a
conversation on screen is the pane plus that panel beside it.
"""


def panel(client, public_id) -> str:
    """The details panel, as the pane loads it."""
    return client.get(
        f"/inbox/{public_id}/panel/", HTTP_HX_REQUEST="true"
    ).content.decode()


def on_screen(client, public_id, **headers) -> str:
    """The conversation (its page, or the pane with ``headers``), then its details panel."""
    page = client.get(f"/inbox/{public_id}/", **headers).content.decode()
    return page + panel(client, public_id)
