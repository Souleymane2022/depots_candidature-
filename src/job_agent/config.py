"""Configuration globale : .env, chemins, valeurs par défaut, fuseau horaire."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

DISPLAY_TZ = ZoneInfo("Europe/Paris")
"""Fuseau horaire pour l'affichage. Stockage interne toujours en UTC."""

DEFAULT_SCORE_THRESHOLD = 60
DEFAULT_RATE_PER_HOUR = 5
DEFAULT_INBOX_HOURS = 24
DEFAULT_CONFIRMATION_TIMEOUT_S = 300
DEFAULT_CONFIRMATION_POLL_S = 30

MODEL_OPUS = "claude-opus-4-7"
"""Modèle Claude pour le scoring, l'écriture de lettres et les réponses Q&A formulaire."""

MODEL_HAIKU = "claude-haiku-4-5-20251001"
"""Modèle Claude pour la classification rapide d'emails entrants."""


@dataclass(frozen=True)
class Config:
    """Configuration résolue (lecture seule)."""

    project_root: Path
    db_path: Path
    profile_path: Path
    emails_path: Path
    logs_dir: Path
    generated_dir: Path
    cache_dir: Path
    anthropic_api_key: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)

    def ensure_dirs(self) -> None:
        """Crée les répertoires data/* nécessaires s'ils n'existent pas."""
        for d in (self.db_path.parent, self.logs_dir, self.generated_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)


def _resolve_path(env_key: str, default: str, root: Path) -> Path:
    raw = os.environ.get(env_key, default)
    p = Path(raw)
    return p if p.is_absolute() else (root / p)


def load(project_root: Path | None = None) -> Config:
    """Charge .env, calcule les chemins et renvoie une Config immuable.

    Args:
        project_root: racine du projet (par défaut, le cwd).
    """
    root = project_root if project_root is not None else Path.cwd()
    load_dotenv(root / ".env", override=False)

    return Config(
        project_root=root,
        db_path=_resolve_path("JOB_AGENT_DB_PATH", "data/agent.db", root),
        profile_path=_resolve_path("JOB_AGENT_PROFILE_PATH", "data/profile.json", root),
        emails_path=_resolve_path("JOB_AGENT_EMAILS_PATH", "data/emails.json", root),
        logs_dir=_resolve_path("JOB_AGENT_LOGS_DIR", "data/logs", root),
        generated_dir=_resolve_path("JOB_AGENT_GENERATED_DIR", "data/generated", root),
        cache_dir=_resolve_path("JOB_AGENT_CACHE_DIR", "data/.cache", root),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
    )
