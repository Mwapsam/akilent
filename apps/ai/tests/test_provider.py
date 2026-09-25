"""The Ollama provider, the registry and the proposal contract."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from apps.ai import proposals
from apps.ai.providers import AIProviderError, get_ai_provider, is_configured
from apps.ai.providers.ollama import OllamaProvider
from apps.ai.types import ChatMessage


def _response(status=200, body=None):
    r = MagicMock(status_code=status)
    r.json.return_value = body if body is not None else {
        "model": "gpt-oss:120b", "message": {"role": "assistant", "content": "Hello!", "thinking": "hmm"},
        "prompt_eval_count": 12, "eval_count": 3}
    return r


def test_ollama_sends_the_chat_request_with_the_key_and_reads_only_the_answer():
    provider = OllamaProvider(base_url="https://ollama.com/", api_key="secret", model="gpt-oss:120b", timeout=30)
    with patch("apps.ai.providers.ollama.requests.post", return_value=_response()) as post:
        result = provider.chat([ChatMessage("user", "Hi")], system="Be kind", max_tokens=100, temperature=0.1)
    url, kwargs = post.call_args.args[0], post.call_args.kwargs
    assert url == "https://ollama.com/api/chat"
    assert kwargs["headers"]["Authorization"] == "Bearer secret"
    assert kwargs["json"]["stream"] is False and kwargs["json"]["model"] == "gpt-oss:120b"
    assert kwargs["json"]["messages"] == [{"role": "system", "content": "Be kind"}, {"role": "user", "content": "Hi"}]
    assert kwargs["json"]["options"] == {"temperature": 0.1, "num_predict": 100}
    assert kwargs["timeout"] == (5, 30)
    assert result.text == "Hello!" and result.usage == {"input_tokens": 12, "output_tokens": 3}


def test_no_key_means_no_authorization_header():
    with patch("apps.ai.providers.ollama.requests.post", return_value=_response()) as post:
        OllamaProvider(base_url="http://host.docker.internal:11434", api_key="").chat([ChatMessage("user", "x")])
    assert "Authorization" not in post.call_args.kwargs["headers"]


@pytest.mark.parametrize("status,words", [(401, "API key"), (404, "model"), (429, "rate"), (500, "error")])
def test_http_errors_become_readable_provider_errors(status, words):
    with patch("apps.ai.providers.ollama.requests.post", return_value=_response(status)):
        with pytest.raises(AIProviderError, match=words):
            OllamaProvider(api_key="k").chat([ChatMessage("user", "x")])


def test_timeouts_network_errors_and_empty_answers_are_provider_errors():
    for side in (requests.Timeout(), requests.ConnectionError()):
        with patch("apps.ai.providers.ollama.requests.post", side_effect=side), pytest.raises(AIProviderError):
            OllamaProvider().chat([ChatMessage("user", "x")])
    with patch("apps.ai.providers.ollama.requests.post", return_value=_response(body={"message": {"content": " "}})):
        with pytest.raises(AIProviderError, match="empty"):
            OllamaProvider().chat([ChatMessage("user", "x")])


def test_the_api_key_never_appears_in_an_error(settings):
    with patch("apps.ai.providers.ollama.requests.post", return_value=_response(401)):
        with pytest.raises(AIProviderError) as exc:
            OllamaProvider(api_key="sk-very-secret").chat([ChatMessage("user", "x")])
    assert "sk-very-secret" not in str(exc.value)


def test_registry(settings):
    settings.AI_PROVIDER_BACKEND = "none"
    assert not is_configured()
    with pytest.raises(AIProviderError):
        get_ai_provider()
    settings.AI_PROVIDER_BACKEND = "ollama"
    assert isinstance(get_ai_provider(), OllamaProvider)
    settings.AI_PROVIDER_BACKEND = "apps.nowhere.Nothing"
    with pytest.raises(AIProviderError, match="Unknown"):
        get_ai_provider()


# ---- the proposal contract ----
TEMPLATES = {"checking_in": ["name", "topic"]}


def test_a_reply_proposal_is_parsed_even_inside_a_code_fence():
    text = 'Sure:\n```json\n{"version": 1, "action": "reply", "confidence": 1.7, "reason": "Asked price", "payload": {"text": "From K18,000."}}\n```'
    p = proposals.parse(text, templates=TEMPLATES, window_open=True)
    assert p == {"version": 1, "action": "reply", "confidence": 1.0, "reason": "Asked price",
                 "payload": {"text": "From K18,000."}, "extras": []}


@pytest.mark.parametrize("bad", [
    "no json here",
    '{"version": 2, "action": "reply", "payload": {"text": "x"}}',
    '{"version": 1, "action": "send_money", "payload": {}}',
    '{"version": 1, "action": "reply", "payload": {"text": "  "}}',
])
def test_unusable_answers_are_rejected(bad):
    with pytest.raises(proposals.ProposalError):
        proposals.parse(bad, templates=TEMPLATES, window_open=True)


def test_a_normal_reply_is_refused_once_the_window_has_closed():
    with pytest.raises(proposals.ProposalError, match="24-hour"):
        proposals.parse('{"version":1,"action":"reply","payload":{"text":"hi"}}', templates=TEMPLATES, window_open=False)


def test_template_proposals_must_be_approved_complete_and_never_type_a_name():
    ok = proposals.parse(
        '{"version":1,"action":"send_template","payload":{"template":"checking_in",'
        '"variables":{"name":"contact.first_name","topic":"your solar quote"}}}',
        templates=TEMPLATES, window_open=False)
    assert ok["payload"]["variables"] == {"name": "contact.first_name", "topic": "your solar quote"}
    for payload in (
        '{"template":"made_up","variables":{}}',
        '{"template":"checking_in","variables":{"name":"contact.first_name"}}',
    ):
        with pytest.raises(proposals.ProposalError):
            proposals.parse('{"version":1,"action":"send_template","payload":%s}' % payload,
                            templates=TEMPLATES, window_open=False)


@pytest.mark.parametrize("name_value", ['"Ada"', '""', 'null', '"{{contact.first_name}}"'])
def test_a_name_blank_always_comes_from_the_customers_own_details(name_value):
    # Typed, empty or wrapped in braces: the name is always read from this customer's record.
    p = proposals.parse(
        '{"version":1,"action":"send_template","payload":{"template":"Customer check-in",'
        '"variables":{"Customer name":%s,"topic":"your quote"}}}' % name_value,
        templates={"Customer check-in": ["Customer name", "topic"]}, window_open=False)
    assert p["payload"]["variables"]["Customer name"] == "contact.first_name"
