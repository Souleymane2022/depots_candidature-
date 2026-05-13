"""Couche Anthropic : singleton client, retry exponentiel, 4 fonctions métier.

Toutes les sorties structurées passent par un `tool_use` forcé (un outil = un schéma Pydantic),
ce qui élimine le parsing JSON fragile. Le client est injectable pour faciliter les tests.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from anthropic import Anthropic, APIConnectionError, APIStatusError, RateLimitError

from . import prompts
from .config import MODEL_HAIKU, MODEL_OPUS
from .logging_setup import get_logger
from .models import (
    CoverLetterResult,
    EmailClassification,
    FundingInfo,
    JobOffer,
    LetterLanguageMode,
    Opportunity,
    OpportunityTypeDetection,
    Profile,
    ScoreResult,
)

log = get_logger("llm")

_client: Anthropic | None = None


def get_client() -> Anthropic:
    """Renvoie le client Anthropic singleton (lit ANTHROPIC_API_KEY dans l'env)."""
    global _client
    if _client is None:
        _client = Anthropic()
    return _client


def set_client(client: Anthropic | Any) -> None:
    """Injection de dépendance pour les tests."""
    global _client
    _client = client


def reset_client() -> None:
    global _client
    _client = None


def retry_exponential(
    *,
    retries: int = 3,
    base: float = 2.0,
    sleep_fn: Callable[[float], None] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Décorateur : retry exponentiel sur erreurs transitoires de l'API Anthropic.

    Si sleep_fn est None, time.sleep est résolu dynamiquement à chaque appel
    (permet aux tests de monkeypatcher time.sleep).
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            sleep = sleep_fn if sleep_fn is not None else time.sleep
            last_exc: Exception | None = None
            for attempt in range(retries):
                try:
                    return fn(*args, **kwargs)
                except (RateLimitError, APIConnectionError) as exc:
                    last_exc = exc
                    delay = base ** (attempt + 1)
                    log.warning("LLM transient error %s, retry in %.1fs", type(exc).__name__, delay)
                    sleep(delay)
                except APIStatusError as exc:
                    if 500 <= exc.status_code < 600:
                        last_exc = exc
                        delay = base ** (attempt + 1)
                        log.warning("LLM 5xx %s, retry in %.1fs", exc.status_code, delay)
                        sleep(delay)
                    else:
                        raise
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


def _call_with_tool(
    *,
    model: str,
    user_prompt: str,
    tool_name: str,
    tool_description: str,
    tool_schema: dict[str, Any],
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """Appel Claude avec forced tool_use ; renvoie le dict `input` du tool_use."""
    client = get_client()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        tools=[
            {
                "name": tool_name,
                "description": tool_description,
                "input_schema": tool_schema,
            }
        ],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": user_prompt}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data
            if isinstance(data, str):
                return json.loads(data)
    raise ValueError(f"Aucun tool_use '{tool_name}' dans la réponse du modèle")


_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
        "language_detected": {"type": "string"},
    },
    "required": ["score", "reason", "language_detected"],
}

_LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "language": {"type": "string"},
        "word_count": {"type": "integer", "minimum": 0},
    },
    "required": ["text", "language", "word_count"],
}

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["confirmation", "rejection", "interview_invite", "other"],
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["type", "confidence"],
}

_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


@retry_exponential()
def score_offer(profile: Profile, offer: JobOffer, cv_text: str = "") -> ScoreResult:
    """Score une offre (0-100) selon l'adéquation au profil. Sortie typée."""
    prompt = prompts.render(
        "score_offer",
        profile_json=profile.model_dump_json(),
        cv_text=cv_text[:8000],
        job_title=offer.title,
        company=offer.company,
        job_url=offer.url,
        job_description=offer.description[:8000],
    )
    raw = _call_with_tool(
        model=MODEL_OPUS,
        user_prompt=prompt,
        tool_name="score_offer",
        tool_description="Renvoie un score d'adéquation entre 0 et 100 + raison + langue détectée.",
        tool_schema=_SCORE_SCHEMA,
    )
    return ScoreResult.model_validate(raw)


@retry_exponential()
def write_cover_letter(
    profile: Profile,
    offer: JobOffer,
    language_mode: LetterLanguageMode = LetterLanguageMode.AUTO,
    cv_text: str = "",
) -> CoverLetterResult:
    """Génère une lettre de motivation 200-300 mots, langue auto ou imposée."""
    target = language_mode.value if language_mode != LetterLanguageMode.AUTO else "auto"
    prompt = prompts.render(
        "write_letter",
        profile_json=profile.model_dump_json(),
        cv_text=cv_text[:8000],
        company=offer.company,
        job_title=offer.title,
        job_description=offer.description[:8000],
        target_language=target,
    )
    raw = _call_with_tool(
        model=MODEL_OPUS,
        user_prompt=prompt,
        tool_name="write_cover_letter",
        tool_description="Renvoie une lettre de motivation 200-300 mots, sa langue, et son nb de mots.",
        tool_schema=_LETTER_SCHEMA,
        max_tokens=2048,
    )
    return CoverLetterResult.model_validate(raw)


@retry_exponential()
def classify_email(subject: str, body: str) -> EmailClassification:
    """Classifie un email entrant en confirmation/rejection/interview_invite/other."""
    prompt = prompts.render("classify_email", subject=subject[:500], body=body[:4000])
    raw = _call_with_tool(
        model=MODEL_HAIKU,
        user_prompt=prompt,
        tool_name="classify_email",
        tool_description="Classifie un email post-candidature en 4 catégories + confidence.",
        tool_schema=_CLASSIFY_SCHEMA,
        max_tokens=200,
    )
    return EmailClassification.model_validate(raw)


_DETECT_TYPE_SCHEMA = {
    "type": "object",
    "properties": {
        "opportunity_type": {
            "type": "string",
            "enum": ["job", "contest", "event", "cfp"],
        },
        "deadline_iso": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["opportunity_type", "confidence"],
}

_FUNDING_SCHEMA = {
    "type": "object",
    "properties": {
        "is_free_to_enter": {"type": "boolean"},
        "is_funded": {"type": "boolean"},
        "funding_amount_eur": {"type": ["integer", "null"], "minimum": 0},
        "funding_type": {
            "type": "string",
            "enum": ["prize", "grant", "scholarship", "salary", "none"],
        },
        "notes": {"type": "string"},
    },
    "required": ["is_free_to_enter", "is_funded", "funding_type"],
}


@retry_exponential()
def detect_opportunity_type(
    title: str, organization: str, description: str
) -> OpportunityTypeDetection:
    """Classifie une page en job/contest/event/cfp + extrait deadline si présente."""
    prompt = prompts.render(
        "detect_opportunity_type",
        title=title[:300],
        organization=organization[:200],
        description=description[:6000],
    )
    raw = _call_with_tool(
        model=MODEL_HAIKU,
        user_prompt=prompt,
        tool_name="detect_opportunity_type",
        tool_description="Classifie une opportunité en job/contest/event/cfp et extrait sa deadline.",
        tool_schema=_DETECT_TYPE_SCHEMA,
        max_tokens=300,
    )
    return OpportunityTypeDetection.model_validate(raw)


@retry_exponential()
def extract_funding(title: str, description: str) -> FundingInfo:
    """Extrait les conditions financières d'une opportunité."""
    prompt = prompts.render(
        "extract_funding",
        title=title[:300],
        description=description[:6000],
    )
    raw = _call_with_tool(
        model=MODEL_HAIKU,
        user_prompt=prompt,
        tool_name="extract_funding",
        tool_description="Extrait gratuité, financement, montant et type de financement.",
        tool_schema=_FUNDING_SCHEMA,
        max_tokens=400,
    )
    return FundingInfo.model_validate(raw)


@retry_exponential()
def score_opportunity(
    profile: Profile, opportunity: Opportunity, cv_text: str = ""
) -> ScoreResult:
    """Score une opportunité non-job (concours, event, cfp) sur 0-100."""
    prompt = prompts.render(
        "score_opportunity",
        profile_json=profile.model_dump_json(),
        cv_text=cv_text[:8000],
        opportunity_type=opportunity.opportunity_type.value,
        title=opportunity.title,
        organization=opportunity.organization,
        url=opportunity.url,
        deadline=opportunity.deadline.isoformat() if opportunity.deadline else "non précisée",
        description=opportunity.description[:8000],
    )
    raw = _call_with_tool(
        model=MODEL_OPUS,
        user_prompt=prompt,
        tool_name="score_offer",
        tool_description="Renvoie un score d'adéquation entre 0 et 100 + raison + langue détectée.",
        tool_schema=_SCORE_SCHEMA,
    )
    return ScoreResult.model_validate(raw)


@retry_exponential()
def answer_form_field(profile: Profile, question: str, offer: JobOffer) -> str:
    """Génère une réponse cohérente avec le profil pour un champ de formulaire ouvert."""
    prompt = prompts.render(
        "answer_field",
        profile_json=profile.model_dump_json(),
        company=offer.company,
        job_title=offer.title,
        question=question[:1000],
    )
    raw = _call_with_tool(
        model=MODEL_OPUS,
        user_prompt=prompt,
        tool_name="answer_form_field",
        tool_description="Renvoie une réponse plausible et cohérente avec le profil.",
        tool_schema=_ANSWER_SCHEMA,
        max_tokens=400,
    )
    answer = raw.get("answer", "").strip()
    if not answer:
        raise ValueError("answer_form_field a renvoyé une réponse vide")
    return answer
