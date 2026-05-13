"""Lecture IMAP : attente du mail de confirmation + classification des mails récents.

Mots-passe et adresses ne sont JAMAIS loggés.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from imap_tools import AND, MailBox, MailMessage

from . import llm
from .logging_setup import get_logger
from .models import EmailAccount, EmailEventType, InboxEvent

log = get_logger("imap_reader")

_CONFIRMATION_KEYWORDS = (
    "confirm",
    "verify",
    "verif",
    "activate",
    "activation",
    "confirmer",
    "verifier",
    "vérifier",
    "valider",
)

_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)


@contextmanager
def mailbox(account: EmailAccount) -> Iterator[MailBox]:
    """Contexte de connexion IMAP avec login/logout propres."""
    box = MailBox(account.imap_host, port=account.imap_port).login(
        account.address, account.app_password
    )
    try:
        yield box
    finally:
        try:
            box.logout()
        except Exception:  # noqa: BLE001
            log.debug("imap logout failed (ignored)")


def can_connect(account: EmailAccount) -> bool:
    """Teste si l'authentification IMAP fonctionne. Renvoie True/False."""
    try:
        with mailbox(account):
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("IMAP login failed for %s: %s", _redact(account.address), exc)
        return False


def wait_for_confirmation(
    account: EmailAccount,
    since: datetime,
    timeout: int = 300,
    poll: int = 30,
    sleep_fn: object = time.sleep,
) -> str | None:
    """Attend un mail contenant un lien de confirmation reçu après `since`.

    Args:
        account: boîte à surveiller.
        since: instant à partir duquel chercher (UTC).
        timeout: durée totale max en secondes.
        poll: intervalle entre 2 polls en secondes.

    Returns:
        Première URL contenant un mot-clé de confirmation, ou None si timeout.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    since_date = since.astimezone(UTC).date()
    while datetime.now(UTC) < deadline:
        try:
            with mailbox(account) as box:
                for msg in box.fetch(AND(date_gte=since_date), reverse=True, mark_seen=False):
                    if msg.date < since:
                        continue
                    url = _find_confirmation_url(msg)
                    if url is not None:
                        log.info("Confirmation URL found in %s", _redact(account.address))
                        return url
        except Exception as exc:  # noqa: BLE001
            log.warning("IMAP poll failed for %s: %s", _redact(account.address), exc)
        sleep_fn(poll)  # type: ignore[operator]
    log.info("Confirmation timeout (%ds) for %s", timeout, _redact(account.address))
    return None


def classify_recent(account: EmailAccount, hours: int = 24) -> list[InboxEvent]:
    """Classifie les mails reçus depuis `hours` heures via Claude haiku."""
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    cutoff_date = cutoff.date()
    events: list[InboxEvent] = []
    try:
        with mailbox(account) as box:
            for msg in box.fetch(AND(date_gte=cutoff_date), reverse=True, mark_seen=False):
                if msg.date < cutoff:
                    continue
                body = msg.text or msg.html or ""
                classification = llm.classify_email(msg.subject or "", body)
                events.append(
                    InboxEvent(
                        received_at=msg.date,
                        subject=msg.subject or "",
                        sender=msg.from_ or "",
                        event_type=classification.type,
                        raw_snippet=body[:500],
                        confidence=classification.confidence,
                    )
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("IMAP classify_recent failed for %s: %s", _redact(account.address), exc)
    return events


def _find_confirmation_url(msg: MailMessage) -> str | None:
    """Cherche dans le mail une URL contenant un mot-clé de confirmation."""
    subject = (msg.subject or "").lower()
    body_text = msg.text or msg.html or ""
    body_lower = body_text.lower()
    if not any(kw in subject or kw in body_lower for kw in _CONFIRMATION_KEYWORDS):
        return None
    for url in _URL_RE.findall(body_text):
        if any(kw in url.lower() for kw in _CONFIRMATION_KEYWORDS):
            return url
    matches = _URL_RE.findall(body_text)
    return matches[0] if matches else None


def _find_confirmation_url_in_strings(subject: str, body: str) -> str | None:
    """Variante de _find_confirmation_url exposée pour les tests (sans MailMessage)."""

    class _Msg:
        pass

    m = _Msg()
    m.subject = subject  # type: ignore[attr-defined]
    m.text = body  # type: ignore[attr-defined]
    m.html = None  # type: ignore[attr-defined]
    return _find_confirmation_url(m)  # type: ignore[arg-type]


def _redact(address: str) -> str:
    """Masque la partie locale de l'adresse pour les logs (user@x.com -> u***@x.com)."""
    if "@" not in address:
        return "***"
    local, _, domain = address.partition("@")
    if len(local) <= 1:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


__all__ = [
    "can_connect",
    "classify_recent",
    "mailbox",
    "wait_for_confirmation",
    "EmailEventType",
]
