"""The self-hosted Ollama fallback: basic auth, think flag, and switching over on failure."""

import json
from unittest.mock import MagicMock, patch

import pytest

from apps.ai.providers import (
    AIProviderError,
    fallback_configured,
    get_ai_provider,
    get_fallback_provider,
)
from apps.ai.providers.base import AIProvider, CompletionResult
from apps.ai.providers.fallback import FallbackProvider
from apps.ai.providers.ollama import OllamaProvider
from apps.ai.types import ChatMessage


def _response(model="gemma4:12b"):
    r = MagicMock(status_code=200)
    r.json.return_value = {
        "model": model,
        "message": {"role": "assistant", "content": "Hi there"},
        "prompt_eval_count": 4,
        "eval_count": 2,
    }
    return r


class _Stub(AIProvider):
    def __init__(self, text="", error=""):
        self.text, self.error, self.calls = text, error, []

    def chat(self, messages, system="", max_tokens=1024, temperature=0.3, timeout=None):
        self.calls.append({"timeout": timeout, "system": system})
        if self.error:
            raise AIProviderError(self.error)
        return CompletionResult(text=self.text, model="stub")


@pytest.fixture
def fallback_settings(settings):
    settings.AI_PROVIDER_BACKEND = "ollama"
    settings.AI_FALLBACK_BACKEND = "ollama"
    settings.AI_FALLBACK_BASE_URL = "https://ai.example.test/"
    settings.AI_FALLBACK_MODEL = "gemma4:12b"
    settings.AI_FALLBACK_USERNAME = "ollama-client"
    settings.AI_FALLBACK_PASSWORD = "pw-very-secret"
    settings.AI_FALLBACK_TIMEOUT_SECONDS = 180
    return settings


def test_basic_auth_and_think_are_sent_and_no_bearer_header():
    provider = OllamaProvider(
        base_url="https://ai.example.test",
        api_key="ignored",
        model="gemma4:12b",
        username="ollama-client",
        password="pw",
        think=False,
        timeout=180,
        connect_timeout=15,
    )
    with patch(
        "apps.ai.providers.http.requests.post", return_value=_response()
    ) as post:
        result = provider.chat([ChatMessage("user", "Hi")])
    kwargs = post.call_args.kwargs
    assert post.call_args.args[0] == "https://ai.example.test/api/chat"
    assert kwargs["auth"] == ("ollama-client", "pw")
    assert "Authorization" not in kwargs["headers"]
    assert kwargs["json"]["think"] is False
    assert kwargs["timeout"] == (15, 180)
    assert result.text == "Hi there"


def test_think_is_left_out_unless_set():
    with patch(
        "apps.ai.providers.http.requests.post", return_value=_response()
    ) as post:
        OllamaProvider(api_key="k").chat([ChatMessage("user", "x")])
    assert "think" not in post.call_args.kwargs["json"]
    assert "auth" not in post.call_args.kwargs


def test_primary_success_never_touches_the_fallback():
    primary, backup = _Stub(text="from primary"), _Stub(text="from backup")
    result = FallbackProvider(primary, backup).chat(
        [ChatMessage("user", "x")], timeout=5
    )
    assert result.text == "from primary" and result.provider == ""
    assert backup.calls == []


def test_a_primary_failure_goes_to_the_fallback_with_its_own_timeout():
    primary, backup = _Stub(error="rate limited"), _Stub(text="from backup")
    result = FallbackProvider(primary, backup, fallback_name="ollama").chat(
        [ChatMessage("user", "x")], system="Be kind", timeout=5
    )
    assert result.text == "from backup"
    assert result.provider == "fallback:ollama"
    assert backup.calls == [{"timeout": None, "system": "Be kind"}]


def test_both_failing_raises_with_both_reasons():
    provider = FallbackProvider(
        _Stub(error="Primary down."), _Stub(error="Backup down.")
    )
    with pytest.raises(AIProviderError) as exc:
        provider.chat([ChatMessage("user", "x")])
    assert "Primary down." in str(exc.value) and "Backup down." in str(exc.value)


def test_factory_wraps_the_primary_only_when_a_fallback_is_set(fallback_settings):
    provider = get_ai_provider()
    assert isinstance(provider, FallbackProvider)
    assert isinstance(provider.primary, OllamaProvider)
    backup = provider.fallback
    assert backup.base_url == "https://ai.example.test"
    assert backup.model == "gemma4:12b" and backup.timeout == 180
    assert backup.auth == ("ollama-client", "pw-very-secret") and backup.think is False

    fallback_settings.AI_FALLBACK_BACKEND = ""
    assert not fallback_configured() and get_fallback_provider() is None
    assert isinstance(get_ai_provider(), OllamaProvider)


def test_fallback_alone_does_not_switch_ai_on(fallback_settings):
    fallback_settings.AI_PROVIDER_BACKEND = "none"
    with pytest.raises(AIProviderError, match="not configured"):
        get_ai_provider()


def test_the_password_never_appears_in_an_error(fallback_settings):
    denied = MagicMock(status_code=401)
    with patch("apps.ai.providers.http.requests.post", return_value=denied):
        with pytest.raises(AIProviderError) as exc:
            get_ai_provider().chat([ChatMessage("user", "x")])
    assert "pw-very-secret" not in str(exc.value)


def _streamed(lines, status=200):
    r = MagicMock(status_code=status)
    r.iter_lines.return_value = [json.dumps(x).encode() for x in lines]
    return r


STREAM = [
    {"model": "gemma4:12b", "message": {"content": "Hel"}, "done": False},
    {"model": "gemma4:12b", "message": {"content": "lo!"}, "done": False},
    {
        "model": "gemma4:12b",
        "message": {"content": ""},
        "done": True,
        "prompt_eval_count": 9,
        "eval_count": 2,
    },
]


def test_a_streamed_answer_is_joined_and_the_connection_closed():
    response = _streamed(STREAM)
    provider = OllamaProvider(api_key="", stream=True, idle_timeout=90, timeout=300)
    with patch("apps.ai.providers.http.requests.post", return_value=response) as post:
        result = provider.chat([ChatMessage("user", "x")])
    kwargs = post.call_args.kwargs
    assert kwargs["stream"] is True and kwargs["json"]["stream"] is True
    assert kwargs["timeout"] == (5, 90)
    assert result.text == "Hello!" and result.usage == {
        "input_tokens": 9,
        "output_tokens": 2,
    }
    response.close.assert_called()


def test_a_stream_cut_before_done_is_an_error():
    with patch(
        "apps.ai.providers.http.requests.post", return_value=_streamed(STREAM[:2])
    ):
        with pytest.raises(AIProviderError, match="before finishing"):
            OllamaProvider(stream=True).chat([ChatMessage("user", "x")])


def test_a_stream_past_its_deadline_stops():
    clock = iter([0, 0, 500, 500, 500])
    with (
        patch("apps.ai.providers.http.requests.post", return_value=_streamed(STREAM)),
        patch("apps.ai.providers.http.time.monotonic", lambda: next(clock)),
    ):
        with pytest.raises(AIProviderError, match="too long"):
            OllamaProvider(stream=True, timeout=300).chat([ChatMessage("user", "x")])


def test_a_streamed_error_status_is_still_readable():
    with patch(
        "apps.ai.providers.http.requests.post", return_value=_streamed([], status=524)
    ):
        with pytest.raises(AIProviderError, match="Cloudflare"):
            OllamaProvider(stream=True).chat([ChatMessage("user", "x")])


def test_the_configured_fallback_streams(fallback_settings):
    backup = get_fallback_provider()
    assert backup.stream is True and backup.idle_timeout == 90


def test_only_a_double_failure_is_marked_not_worth_retrying():
    assert AIProviderError("x").retryable is True
    provider = FallbackProvider(_Stub(error="down"), _Stub(error="slow"))
    with pytest.raises(AIProviderError) as exc:
        provider.chat([ChatMessage("user", "x")])
    assert exc.value.retryable is False
