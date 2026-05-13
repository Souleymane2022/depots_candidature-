"""Couche SQLite : bootstrap du schéma, helpers, transactions explicites.

Toutes les écritures multi-tables sont entourées d'une transaction.
Tous les timestamps sont stockés en UTC ISO 8601 (TEXT).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .models import ApplicationStatus, EmailEventType, EmailStatus

log = get_logger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    address TEXT PRIMARY KEY,
    app_password TEXT NOT NULL,
    imap_host TEXT NOT NULL,
    imap_port INTEGER NOT NULL,
    smtp_host TEXT NOT NULL,
    smtp_port INTEGER NOT NULL,
    display_name TEXT,
    last_used_at TIMESTAMP,
    status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_used TEXT REFERENCES emails(address),
    company TEXT NOT NULL,
    job_title TEXT NOT NULL,
    job_url TEXT UNIQUE NOT NULL,
    job_description TEXT,
    score INTEGER,
    score_reason TEXT,
    cover_letter TEXT,
    applied_at TIMESTAMP,
    status TEXT DEFAULT 'pending',
    response_received_at TIMESTAMP,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS email_assignments (
    application_id INTEGER REFERENCES applications(id),
    email TEXT REFERENCES emails(address),
    company TEXT NOT NULL,
    PRIMARY KEY (email, company)
);

CREATE TABLE IF NOT EXISTS inbox_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT REFERENCES emails(address),
    application_id INTEGER REFERENCES applications(id),
    received_at TIMESTAMP,
    subject TEXT,
    sender TEXT,
    event_type TEXT,
    raw_snippet TEXT
);

CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_applications_company ON applications(company);
CREATE INDEX IF NOT EXISTS idx_inbox_events_app ON inbox_events(application_id);
"""


def _now_utc_iso() -> str:
    """Renvoie l'instant courant en UTC ISO 8601 (secondes)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    """Ouvre une connexion SQLite avec foreign keys et row_factory."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def bootstrap(db_path: Path) -> sqlite3.Connection:
    """Crée le schéma si absent, renvoie une connexion prête à l'emploi."""
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    log.debug("DB bootstrap OK at %s", db_path)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Contexte de transaction SQLite (BEGIN/COMMIT/ROLLBACK)."""
    conn.execute("BEGIN")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def upsert_email(conn: sqlite3.Connection, account: dict[str, Any]) -> None:
    """Insère ou met à jour une boîte email du pool (préserve last_used_at)."""
    conn.execute(
        """
        INSERT INTO emails (address, app_password, imap_host, imap_port,
                            smtp_host, smtp_port, display_name, status)
        VALUES (:address, :app_password, :imap_host, :imap_port,
                :smtp_host, :smtp_port, :display_name, :status)
        ON CONFLICT(address) DO UPDATE SET
            app_password = excluded.app_password,
            imap_host = excluded.imap_host,
            imap_port = excluded.imap_port,
            smtp_host = excluded.smtp_host,
            smtp_port = excluded.smtp_port,
            display_name = excluded.display_name,
            status = excluded.status
        """,
        {
            "address": account["address"],
            "app_password": account["app_password"],
            "imap_host": account["imap_host"],
            "imap_port": account["imap_port"],
            "smtp_host": account["smtp_host"],
            "smtp_port": account["smtp_port"],
            "display_name": account.get("display_name"),
            "status": account.get("status", EmailStatus.ACTIVE.value),
        },
    )


def set_email_status(conn: sqlite3.Connection, address: str, status: EmailStatus) -> None:
    conn.execute("UPDATE emails SET status = ? WHERE address = ?", (status.value, address))


def mark_email_used(conn: sqlite3.Connection, address: str) -> None:
    conn.execute(
        "UPDATE emails SET last_used_at = ? WHERE address = ?",
        (_now_utc_iso(), address),
    )


def get_active_emails(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM emails WHERE status = 'active' ORDER BY address"))


def get_all_emails(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM emails ORDER BY address"))


def email_already_used_for_company(conn: sqlite3.Connection, email: str, company: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM email_assignments WHERE email = ? AND company = ?",
        (email, company),
    ).fetchone()
    return row is not None


def get_assigned_email_for_company(
    conn: sqlite3.Connection, company: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT e.* FROM email_assignments ea
        JOIN emails e ON e.address = ea.email
        WHERE ea.company = ? AND e.status = 'active'
        LIMIT 1
        """,
        (company,),
    ).fetchone()


def pick_least_recent_unused_email(
    conn: sqlite3.Connection, company: str
) -> sqlite3.Row | None:
    """Email actif n'ayant pas encore postulé chez cette company, le plus ancien d'abord."""
    return conn.execute(
        """
        SELECT * FROM emails
        WHERE status = 'active'
          AND address NOT IN (SELECT email FROM email_assignments WHERE company = ?)
        ORDER BY (last_used_at IS NULL) DESC, last_used_at ASC
        LIMIT 1
        """,
        (company,),
    ).fetchone()


def record_email_assignment(
    conn: sqlite3.Connection, application_id: int | None, email: str, company: str
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO email_assignments (application_id, email, company)
        VALUES (?, ?, ?)
        """,
        (application_id, email, company),
    )


def find_application_by_url(conn: sqlite3.Connection, job_url: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM applications WHERE job_url = ?", (job_url,)).fetchone()


def insert_application(
    conn: sqlite3.Connection,
    *,
    company: str,
    job_title: str,
    job_url: str,
    job_description: str | None = None,
    status: ApplicationStatus = ApplicationStatus.PENDING,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO applications (company, job_title, job_url, job_description, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (company, job_title, job_url, job_description, status.value),
    )
    return int(cur.lastrowid or 0)


def update_application(conn: sqlite3.Connection, app_id: int, **fields: Any) -> None:
    if not fields:
        return
    if "status" in fields and isinstance(fields["status"], ApplicationStatus):
        fields["status"] = fields["status"].value
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [app_id]
    conn.execute(f"UPDATE applications SET {set_clause} WHERE id = ?", values)


def list_pending(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM applications WHERE status = 'pending' ORDER BY id ASC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql))


def list_recent(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM applications ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
    )


def record_inbox_event(
    conn: sqlite3.Connection,
    *,
    email: str,
    application_id: int | None,
    received_at: datetime,
    subject: str,
    sender: str,
    event_type: EmailEventType,
    raw_snippet: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO inbox_events
            (email, application_id, received_at, subject, sender, event_type, raw_snippet)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            email,
            application_id,
            received_at.astimezone(UTC).replace(microsecond=0).isoformat(),
            subject,
            sender,
            event_type.value,
            raw_snippet[:1000],
        ),
    )
    return int(cur.lastrowid or 0)
