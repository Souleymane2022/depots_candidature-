"""Pool d'emails : rotation intelligente avec contrainte 1 email max par company.

Règles (spec brief) :
- Si une entrée (email, company) existe déjà dans email_assignments -> renvoie ce même email.
- Sinon, prend l'email actif avec last_used_at le plus ancien qui n'a pas postulé chez cette company.
- Si tous les emails ont déjà postulé chez cette company, renvoie None (refus explicite).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pydantic import TypeAdapter

from . import db
from .logging_setup import get_logger
from .models import EmailAccount, EmailStatus

log = get_logger("email_pool")

_EMAIL_LIST_ADAPTER: TypeAdapter[list[EmailAccount]] = TypeAdapter(list[EmailAccount])


class EmailPool:
    """Couche métier au-dessus de la table emails."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def import_from_json(self, path: Path) -> int:
        """Charge data/emails.json et upsert chaque entrée. Renvoie le nombre d'entrées vues."""
        raw = path.read_text(encoding="utf-8")
        accounts = _EMAIL_LIST_ADAPTER.validate_json(raw)
        with db.transaction(self.conn):
            for acc in accounts:
                db.upsert_email(self.conn, acc.model_dump(mode="json"))
        log.info("Pool: imported %d emails from %s", len(accounts), path)
        return len(accounts)

    def list_all(self) -> list[EmailAccount]:
        return [_row_to_account(r) for r in db.get_all_emails(self.conn)]

    def list_active(self) -> list[EmailAccount]:
        return [_row_to_account(r) for r in db.get_active_emails(self.conn)]

    def pick_email_for_company(self, company: str) -> EmailAccount | None:
        """Retourne l'email à utiliser pour cette company, ou None si pool épuisé.

        Persiste l'assignation (email, company) dès qu'elle est choisie.
        """
        company = company.strip()
        if not company:
            raise ValueError("company doit être non vide")

        existing = db.get_assigned_email_for_company(self.conn, company)
        if existing is not None:
            log.debug("Pool: sticky assignment for company=%r -> %s", company, existing["address"])
            return _row_to_account(existing)

        candidate = db.pick_least_recent_unused_email(self.conn, company)
        if candidate is None:
            log.warning("Pool: exhausted for company=%r", company)
            return None

        with db.transaction(self.conn):
            db.record_email_assignment(self.conn, None, candidate["address"], company)
        log.info("Pool: new assignment company=%r -> %s", company, candidate["address"])
        return _row_to_account(candidate)

    def mark_used(self, address: str) -> None:
        """Met à jour last_used_at de l'email à maintenant (UTC)."""
        with db.transaction(self.conn):
            db.mark_email_used(self.conn, address)

    def set_status(self, address: str, status: EmailStatus) -> None:
        with db.transaction(self.conn):
            db.set_email_status(self.conn, address, status)


def _row_to_account(row: sqlite3.Row) -> EmailAccount:
    """Convertit une ligne sqlite en EmailAccount typé."""
    data = dict(row)
    raw_status = data.get("status") or EmailStatus.ACTIVE.value
    data["status"] = EmailStatus(raw_status)
    return EmailAccount.model_validate(data)


def load_raw_emails(path: Path) -> list[dict]:
    """Helper utilitaire pour les tests/CLI : lit le JSON brut sans toucher à la DB."""
    return json.loads(path.read_text(encoding="utf-8"))
