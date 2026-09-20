"""View-model for the WhatsApp setup console.

Templates render this; they never inspect tokens, PINs or registration state
themselves. Setup progress is sequential: the first incomplete required step is
``current`` and is the only one that carries a primary action.
"""
import time
from dataclasses import dataclass, field

DONE, CURRENT, UPCOMING, OPTIONAL = "done", "current", "upcoming", "optional"

CONNECT_URL = "/whatsapp/connect/redirect/"


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


@dataclass
class Step:
    key: str
    label: str
    hint: str
    done: bool
    required: bool = True
    state: str = UPCOMING
    action: dict | None = None  # {label, url, method}


@dataclass
class SetupConsole:
    number: object | None
    steps: list[Step] = field(default_factory=list)
    current: Step | None = None

    @property
    def primary_action(self):
        return self.current.action if self.current else None

    @property
    def required_complete(self) -> bool:
        return all(s.done for s in self.steps if s.required)


def pick_setup_number(numbers):
    """The number the console is about: first one not ready, else the first."""
    for n in numbers:
        if not n.is_ready:
            return n
    return numbers[0] if numbers else None


def build_setup_console(numbers, *, embedded_enabled: bool, inbound_seen: bool) -> SetupConsole:
    number = pick_setup_number(numbers)
    connect = (
        {"label": "Connect with WhatsApp", "url": CONNECT_URL, "method": "get"}
        if embedded_enabled
        else None
    )
    has_creds = bool(number and number.access_token and number.waba_id)

    register = None
    if number and has_creds:
        failed = number.registration_status == number.RegistrationStatus.FAILED
        register = {
            "label": "Retry registration" if failed else "Register number",
            "url": f"/whatsapp/numbers/{number.pk}/register/",
            "method": "post",
        }

    ready = bool(number and number.is_ready)
    tested = bool(ready and number.last_successful_test())
    inbound = None
    if ready and not tested:
        from apps.whatsapp.verification import recent_inbound

        inbound = recent_inbound(number)

    display = (number.display_number or "").strip() if number else ""
    if inbound is not None:
        message_hint = (
            f"We received a message from {inbound.contact.phone_number}. "
            "Send the test to that number."
        )
    elif display:
        message_hint = (
            f"From your phone, send “Hi” to {display} on WhatsApp. This lets us send "
            "a plain-text test on any number and confirms incoming messages work."
        )
    else:
        message_hint = (
            "From your phone, send “Hi” to this WhatsApp number. This lets us send "
            "a plain-text test on any number and confirms incoming messages work."
        )
    message_action = None
    if ready:
        message_action = {
            "kind": "guide", "label": "Open WhatsApp",
            "url": f"https://wa.me/{_digits(display)}?text=Hi" if _digits(display) else "",
            "number": display,
            "status_url": f"/whatsapp/numbers/{number.pk}/status/",
            "since": int(time.time()),
            "help_url": "/help/whatsapp-setup/",
            "verify_url": f"/whatsapp/numbers/{number.pk}/verify/",
        }

    steps = [
        Step(
            "connected", "WhatsApp connected",
            "Use “Connect with WhatsApp” below, or add a number manually.",
            done=number is not None, action=connect,
        ),
        Step(
            "credentials", "Credentials valid",
            "We need an access token and WhatsApp Business Account for this number.",
            done=has_creds, action=connect,
        ),
        Step(
            "registered", "Number registered",
            "Registered on the Cloud API so it can send messages.",
            done=bool(number and number.is_ready), action=register,
        ),
        Step(
            "message_first", "Message this number from your phone", message_hint,
            done=tested or inbound is not None, action=message_action,
        ),
        Step(
            "test", "Send a test message",
            "Confirm that messages can be sent from this number.",
            done=tested,
            action=(
                {
                    "label": "Send test message", "kind": "verify",
                    "url": f"/whatsapp/numbers/{number.pk}/verify/", "method": "post",
                    "prefill": inbound.contact.phone_number if inbound is not None else "",
                }
                if ready else None
            ),
        ),
        Step(
            "incoming", "Confirm incoming messages",
            "Reply to the test message from your phone to confirm inbound works.",
            done=inbound_seen or bool(number and number.has_received_test_reply()),
            required=False,
        ),
    ]

    current = None
    for s in steps:
        if s.done:
            s.state = DONE
        elif not s.required:
            s.state = OPTIONAL
        elif current is None:
            s.state = CURRENT
            current = s
        else:
            s.state = UPCOMING
    return SetupConsole(number=number, steps=steps, current=current)
