"""Tests du module llm : structured output via tool_use, retry exponentiel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from anthropic import APIConnectionError, APIStatusError, RateLimitError

from job_agent import llm
from job_agent.models import (
    JobOffer,
    LetterLanguageMode,
    Profile,
)


@dataclass
class _ToolUseBlock:
    type: str = "tool_use"
    name: str = ""
    input: dict[str, Any] | None = None


@dataclass
class _FakeResponse:
    content: list[Any]


class _FakeMessages:
    def __init__(self, parent: _FakeClient) -> None:
        self._parent = parent

    def create(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        self._parent.calls.append(kwargs)
        if self._parent.errors_to_raise:
            exc = self._parent.errors_to_raise.pop(0)
            if exc is not None:
                raise exc
        payload = self._parent.next_payloads.pop(0)
        return _FakeResponse(content=[_ToolUseBlock(name=kwargs["tools"][0]["name"], input=payload)])


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.errors_to_raise: list[Exception | None] = []
        self.next_payloads: list[dict[str, Any]] = []
        self.messages = _FakeMessages(self)


@pytest.fixture
def fake_client() -> _FakeClient:
    c = _FakeClient()
    llm.set_client(c)
    return c


@pytest.fixture
def profile(sample_profile_dict: dict) -> Profile:
    return Profile.model_validate(sample_profile_dict)


@pytest.fixture
def offer() -> JobOffer:
    return JobOffer(
        url="https://example.com/job/1",
        title="Backend Engineer",
        company="Acme",
        description="We are looking for a Python backend engineer with 5+ years XP.",
    )


def test_score_offer_parses_tool_use(fake_client, profile, offer):
    fake_client.next_payloads = [
        {"score": 82, "reason": "skills match", "language_detected": "en"}
    ]
    result = llm.score_offer(profile, offer)
    assert result.score == 82
    assert result.reason == "skills match"
    assert result.language_detected == "en"


def test_score_offer_validates_score_range(fake_client, profile, offer):
    from pydantic import ValidationError

    fake_client.next_payloads = [
        {"score": 150, "reason": "x", "language_detected": "en"}
    ]
    with pytest.raises(ValidationError):
        llm.score_offer(profile, offer)


def test_write_cover_letter_parses_tool_use(fake_client, profile, offer):
    fake_client.next_payloads = [
        {"text": "Madame, Monsieur, ...", "language": "fr", "word_count": 250}
    ]
    result = llm.write_cover_letter(
        profile, offer, language_mode=LetterLanguageMode.AUTO
    )
    assert result.language == "fr"
    assert result.word_count == 250


def test_classify_email_uses_haiku_model(fake_client):
    fake_client.next_payloads = [{"type": "confirmation", "confidence": 0.91}]
    result = llm.classify_email("Confirm your application", "Click the link below.")
    assert result.type.value == "confirmation"
    assert 0.0 <= result.confidence <= 1.0
    # Vérifie qu'on utilise bien haiku pour la classification
    assert "haiku" in fake_client.calls[0]["model"]


def test_score_offer_uses_opus_model(fake_client, profile, offer):
    fake_client.next_payloads = [
        {"score": 70, "reason": "ok", "language_detected": "fr"}
    ]
    llm.score_offer(profile, offer)
    assert "opus" in fake_client.calls[0]["model"]


def test_retry_on_rate_limit(fake_client, profile, offer, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda d: sleeps.append(d))

    rate_limit = RateLimitError(
        message="rate limited",
        response=_make_dummy_response(429),
        body=None,
    )
    fake_client.errors_to_raise = [rate_limit, rate_limit, None]
    fake_client.next_payloads = [
        {"score": 75, "reason": "ok", "language_detected": "fr"}
    ]
    result = llm.score_offer(profile, offer)
    assert result.score == 75
    assert sleeps == [2.0, 4.0]


def test_retry_gives_up_after_max(fake_client, profile, offer):
    sleeps: list[float] = []
    err = APIConnectionError(message="net down", request=_make_dummy_request())

    @llm.retry_exponential(retries=3, sleep_fn=sleeps.append)
    def always_fails() -> None:
        raise err

    with pytest.raises(APIConnectionError):
        always_fails()
    assert len(sleeps) == 3


def test_retry_does_not_swallow_4xx(fake_client, profile, offer):
    @llm.retry_exponential(retries=3, sleep_fn=lambda _: None)
    def raises_4xx() -> None:
        raise APIStatusError(
            message="bad request",
            response=_make_dummy_response(400),
            body=None,
        )

    with pytest.raises(APIStatusError):
        raises_4xx()


def test_retry_retries_5xx(fake_client):
    sleeps: list[float] = []
    err = APIStatusError(
        message="server down",
        response=_make_dummy_response(503),
        body=None,
    )

    attempts = {"n": 0}

    @llm.retry_exponential(retries=3, sleep_fn=sleeps.append)
    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise err
        return "ok"

    assert flaky() == "ok"
    assert len(sleeps) == 2


def test_answer_form_field_returns_string(fake_client, profile, offer):
    fake_client.next_payloads = [{"answer": "5 years"}]
    answer = llm.answer_form_field(profile, "Years of Python?", offer)
    assert answer == "5 years"


def test_answer_form_field_empty_raises(fake_client, profile, offer):
    fake_client.next_payloads = [{"answer": "   "}]
    with pytest.raises(ValueError):
        llm.answer_form_field(profile, "Q?", offer)


# ---------- helpers ----------


def _make_dummy_response(status: int):
    import httpx

    return httpx.Response(status, request=_make_dummy_request())


def _make_dummy_request():
    import httpx

    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")
