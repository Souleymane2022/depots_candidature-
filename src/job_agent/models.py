"""Schémas Pydantic v2 partagés par tous les modules.

Tous les objets passant entre couches (LLM, DB, browser, CLI) sont définis ici.
Aucune logique métier : seulement validation et typage.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class EmailStatus(StrEnum):
    """État d'une boîte email du pool."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    INVALID = "invalid"


class EmailAccount(BaseModel):
    """Boîte email utilisable par le pool pour postuler."""

    model_config = ConfigDict(extra="forbid")

    address: str = Field(..., description="Adresse complète, ex: user@gmail.com")
    app_password: str = Field(..., description="Mot de passe d'application (jamais loggé)")
    imap_host: str
    imap_port: int = Field(..., gt=0, lt=65536)
    smtp_host: str
    smtp_port: int = Field(..., gt=0, lt=65536)
    display_name: str | None = None
    last_used_at: datetime | None = None
    status: EmailStatus = EmailStatus.ACTIVE

    @field_validator("address")
    @classmethod
    def _address_has_at(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError("address doit contenir un '@'")
        return v.strip().lower()


class LanguageLevel(StrEnum):
    NATIVE = "native"
    FLUENT = "fluent"
    PROFESSIONAL = "professional"
    INTERMEDIATE = "intermediate"
    BASIC = "basic"


class Language(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    level: LanguageLevel


class RemoteMode(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote: RemoteMode = RemoteMode.HYBRID
    salary_min_eur: int = Field(0, ge=0)
    contract_types: list[str] = Field(default_factory=list)
    excluded_industries: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    """Profil du candidat, chargé depuis data/profile.json."""

    model_config = ConfigDict(extra="forbid")

    first_name: str
    last_name: str
    phone: str
    city: str
    country: str
    linkedin: HttpUrl | None = None
    github: HttpUrl | None = None
    portfolio: HttpUrl | None = None
    years_experience: int = Field(..., ge=0)
    current_title: str
    target_roles: list[str] = Field(
        default_factory=list,
        description="Postes visés. Si vide, le scoring se base sur current_title + skills.",
    )
    skills: list[str]
    languages: list[Language]
    preferences: Preferences
    cv_path: str
    bio_short: str


class JobOffer(BaseModel):
    """Offre d'emploi scrapée."""

    model_config = ConfigDict(extra="forbid")

    url: str
    title: str
    company: str
    description: str
    language: str | None = None


class ScoreResult(BaseModel):
    """Sortie de llm.score_offer."""

    model_config = ConfigDict(extra="forbid")

    score: int = Field(..., ge=0, le=100)
    reason: str
    language_detected: str


class CoverLetterResult(BaseModel):
    """Sortie de llm.write_cover_letter."""

    model_config = ConfigDict(extra="forbid")

    text: str
    language: str
    word_count: int = Field(..., ge=0)


class EmailEventType(StrEnum):
    CONFIRMATION = "confirmation"
    REJECTION = "rejection"
    INTERVIEW_INVITE = "interview_invite"
    OTHER = "other"


class EmailClassification(BaseModel):
    """Sortie de llm.classify_email."""

    model_config = ConfigDict(extra="forbid")

    type: EmailEventType
    confidence: float = Field(..., ge=0.0, le=1.0)


class InboxEvent(BaseModel):
    """Email entrant classifié, prêt à être inséré en DB."""

    model_config = ConfigDict(extra="forbid")

    received_at: datetime
    subject: str
    sender: str
    event_type: EmailEventType
    raw_snippet: str
    confidence: float = Field(..., ge=0.0, le=1.0)


class SubmissionResult(BaseModel):
    """Sortie de browser.submit_application."""

    model_config = ConfigDict(extra="forbid")

    submitted: bool
    evidence: str
    error: str | None = None


class ApplicationStatus(StrEnum):
    PENDING = "pending"
    SCORED = "scored"
    REJECTED = "rejected"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    INTERVIEW = "interview"


class ApplicationRecord(BaseModel):
    """Représentation typée d'une ligne de la table applications."""

    model_config = ConfigDict(extra="forbid")

    id: int
    email_used: str | None
    company: str
    job_title: str
    job_url: str
    job_description: str | None
    score: int | None
    score_reason: str | None
    cover_letter: str | None
    applied_at: datetime | None
    status: ApplicationStatus
    response_received_at: datetime | None
    notes: str | None


FormQA = tuple[str, str]
"""Paire (question_clé, réponse) pré-remplie pour les champs récurrents du formulaire."""


class LetterLanguageMode(StrEnum):
    """Mode de choix de la langue de la lettre de motivation."""

    AUTO = "auto"
    FR = "fr"
    EN = "en"
