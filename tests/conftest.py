"""Fixtures partagées."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from job_agent import db, llm


@pytest.fixture
def temp_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Connexion SQLite isolée dans un fichier temporaire."""
    conn = db.bootstrap(tmp_path / "agent.db")
    yield conn
    conn.close()


@pytest.fixture
def sample_emails() -> list[dict]:
    return [
        {
            "address": "alice@example.com",
            "app_password": "xxxx xxxx xxxx xxxx",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "display_name": "Alice",
        },
        {
            "address": "bob@example.com",
            "app_password": "yyyy yyyy yyyy yyyy",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "display_name": "Bob",
        },
        {
            "address": "carol@example.com",
            "app_password": "zzzz zzzz zzzz zzzz",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "display_name": "Carol",
        },
    ]


@pytest.fixture
def emails_json(tmp_path: Path, sample_emails: list[dict]) -> Path:
    p = tmp_path / "emails.json"
    p.write_text(json.dumps(sample_emails), encoding="utf-8")
    return p


@pytest.fixture
def sample_profile_dict() -> dict:
    return {
        "first_name": "Jane",
        "last_name": "Doe",
        "phone": "+33600000000",
        "city": "Paris",
        "country": "France",
        "linkedin": "https://www.linkedin.com/in/janedoe",
        "github": "https://github.com/janedoe",
        "portfolio": None,
        "years_experience": 5,
        "current_title": "Backend Engineer",
        "target_roles": ["Backend Engineer", "Platform Engineer"],
        "skills": ["Python", "SQL", "Docker"],
        "languages": [{"name": "French", "level": "native"}],
        "preferences": {
            "remote": "hybrid",
            "salary_min_eur": 50000,
            "contract_types": ["CDI"],
            "excluded_industries": ["tobacco"],
        },
        "cv_path": "data/cv_base.pdf",
        "bio_short": "Backend engineer with 5 years of experience.",
    }


@pytest.fixture(autouse=True)
def reset_llm_client() -> Iterator[None]:
    """Garantit qu'aucun client réel ne fuit entre tests."""
    llm.reset_client()
    yield
    llm.reset_client()
