"""One-off GA4 events that happen on the server (an account created) but have to be sent
from the browser. A view queues the event in the session; the next page that renders
analytics/google.html sends it once and drops it.

Only an event name and short, non-personal parameters (a plan slug, never an email)."""

SESSION_KEY = "ga_event"


def queue_ga_event(request, name: str, **params) -> None:
    request.session[SESSION_KEY] = {"name": name, "params": params}


def pop_ga_event(request) -> dict | None:
    session = getattr(request, "session", None)
    if session is None:
        return None
    return session.pop(SESSION_KEY, None)
