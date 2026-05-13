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


# ---------------------------------------------------------------------------
# Add-on : recherche d'opportunités + auth
# ---------------------------------------------------------------------------


class OpportunityType(StrEnum):
    """Type d'opportunité que l'agent peut traiter."""

    JOB = "job"
    CONTEST = "contest"
    EVENT = "event"
    CFP = "cfp"


class FundingType(StrEnum):
    PRIZE = "prize"
    GRANT = "grant"
    SCHOLARSHIP = "scholarship"
    SALARY = "salary"
    NONE = "none"


class FundingInfo(BaseModel):
    """Extraction des conditions financières d'une opportunité."""

    model_config = ConfigDict(extra="forbid")

    is_free_to_enter: bool
    is_funded: bool
    funding_amount_eur: int | None = None
    funding_type: FundingType = FundingType.NONE
    notes: str = ""


class OpportunityTypeDetection(BaseModel):
    """Sortie de llm.detect_opportunity_type."""

    model_config = ConfigDict(extra="forbid")

    opportunity_type: OpportunityType
    deadline_iso: str | None = None
    confidence: float = Field(..., ge=0.0, le=1.0)


class Opportunity(BaseModel):
    """Opportunité découverte par une source (job, contest, event, cfp)."""

    model_config = ConfigDict(extra="forbid")

    url: str
    title: str
    organization: str
    description: str
    opportunity_type: OpportunityType = OpportunityType.JOB
    deadline: datetime | None = None
    source: str
    language: str | None = None
    raw_metadata: dict = Field(default_factory=dict)


class WatchSourceMethod(StrEnum):
    RSS = "rss"
    HTML_SCRAPE = "html_scrape"
    BROWSER_USE = "browser_use"
    API_ADZUNA = "api_adzuna"
    API_FRANCE_TRAVAIL = "api_france_travail"


class WatchSource(BaseModel):
    """Source à veiller pour découvrir des opportunités."""

    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    method: WatchSourceMethod
    opportunity_type: OpportunityType = OpportunityType.JOB
    refresh_hours: int = Field(24, gt=0)
    free_funded_only: bool = False
    enabled: bool = True
    last_run_at: datetime | None = None
    notes: str = ""


class SearchQuery(BaseModel):
    """Paramètres d'une recherche multi-source."""

    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = Field(default_factory=list)
    location: str | None = None
    opportunity_types: list[OpportunityType] = Field(default_factory=list)
    max_results_per_source: int = 50
    posted_within_days: int = 14
    free_only: bool = False
    min_funding_eur: int = 0


class AuthMethod(StrEnum):
    """Stratégie d'authentification pour un site."""

    SHARED_CHROME = "shared_chrome"
    STORED = "stored"
    OAUTH = "oauth"
    NONE = "none"


class SiteCredential(BaseModel):
    """Credentials stockés (chiffrés) pour un domaine."""

    model_config = ConfigDict(extra="forbid")

    site_domain: str
    auth_method: AuthMethod
    username: str | None = None
    password: str | None = None
    notes: str = ""
