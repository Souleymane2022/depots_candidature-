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

CREATE TABLE IF NOT EXISTS site_credentials (
    site_domain TEXT PRIMARY KEY,
    auth_method TEXT NOT NULL,
    username TEXT,
    password_encrypted BLOB,
    notes TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS auth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_domain TEXT,
    event_type TEXT,
    success INTEGER,
    occurred_at TIMESTAMP,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS watch_sources (
    name TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    method TEXT NOT NULL,
    opportunity_type TEXT DEFAULT 'job',
    refresh_hours INTEGER DEFAULT 24,
    free_funded_only INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    last_run_at TIMESTAMP,
    notes TEXT
);
"""

_MIGRATIONS = [
    "ALTER TABLE applications ADD COLUMN opportunity_type TEXT DEFAULT 'job'",
    "ALTER TABLE applications ADD COLUMN deadline TIMESTAMP",
    "ALTER TABLE applications ADD COLUMN source TEXT",
]


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
    """Crée le schéma si absent, applique les migrations additives, renvoie la connexion."""
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    _apply_migrations(conn)
    log.debug("DB bootstrap OK at %s", db_path)
    return conn


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Applique les ALTER TABLE additifs en ignorant ceux déjà appliqués."""
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                continue
            raise


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


# ---------------------------------------------------------------------------
# Helpers pour les nouvelles tables (site_credentials, auth_events, watch_sources)
# ---------------------------------------------------------------------------


def upsert_site_credential(
    conn: sqlite3.Connection,
    *,
    site_domain: str,
    auth_method: str,
    username: str | None,
    password_encrypted: bytes | None,
    notes: str = "",
) -> None:
    now = _now_utc_iso()
    conn.execute(
        """
        INSERT INTO site_credentials
            (site_domain, auth_method, username, password_encrypted, notes,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(site_domain) DO UPDATE SET
            auth_method = excluded.auth_method,
            username = excluded.username,
            password_encrypted = excluded.password_encrypted,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (site_domain, auth_method, username, password_encrypted, notes, now, now),
    )


def get_site_credential(conn: sqlite3.Connection, site_domain: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM site_credentials WHERE site_domain = ?", (site_domain,)
    ).fetchone()


def list_site_credentials(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM site_credentials ORDER BY site_domain")
    )


def delete_site_credential(conn: sqlite3.Connection, site_domain: str) -> int:
    cur = conn.execute(
        "DELETE FROM site_credentials WHERE site_domain = ?", (site_domain,)
    )
    return cur.rowcount


def record_auth_event(
    conn: sqlite3.Connection,
    *,
    site_domain: str,
    event_type: str,
    success: bool,
    notes: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO auth_events (site_domain, event_type, success, occurred_at, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (site_domain, event_type, 1 if success else 0, _now_utc_iso(), notes),
    )


def upsert_watch_source(
    conn: sqlite3.Connection,
    *,
    name: str,
    url: str,
    method: str,
    opportunity_type: str = "job",
    refresh_hours: int = 24,
    free_funded_only: bool = False,
    enabled: bool = True,
    notes: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO watch_sources
            (name, url, method, opportunity_type, refresh_hours,
             free_funded_only, enabled, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            url = excluded.url,
            method = excluded.method,
            opportunity_type = excluded.opportunity_type,
            refresh_hours = excluded.refresh_hours,
            free_funded_only = excluded.free_funded_only,
            enabled = excluded.enabled,
            notes = excluded.notes
        """,
        (
            name,
            url,
            method,
            opportunity_type,
            int(refresh_hours),
            1 if free_funded_only else 0,
            1 if enabled else 0,
            notes,
        ),
    )


def get_watch_source(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM watch_sources WHERE name = ?", (name,)).fetchone()


def list_watch_sources(conn: sqlite3.Connection, only_enabled: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM watch_sources"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY name"
    return list(conn.execute(sql))


def set_watch_source_enabled(
    conn: sqlite3.Connection, name: str, enabled: bool
) -> int:
    cur = conn.execute(
        "UPDATE watch_sources SET enabled = ? WHERE name = ?",
        (1 if enabled else 0, name),
    )
    return cur.rowcount


def delete_watch_source(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute("DELETE FROM watch_sources WHERE name = ?", (name,))
    return cur.rowcount


def mark_watch_source_run(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "UPDATE watch_sources SET last_run_at = ? WHERE name = ?",
        (_now_utc_iso(), name),
    )


def insert_opportunity_application(
    conn: sqlite3.Connection,
    *,
    company: str,
    job_title: str,
    job_url: str,
    job_description: str | None,
    opportunity_type: str,
    deadline: datetime | None,
    source: str,
) -> int:
    """Insert d'une opportunité (job/contest/event/cfp) dans applications."""
    cur = conn.execute(
        """
        INSERT INTO applications
            (company, job_title, job_url, job_description, status,
             opportunity_type, deadline, source)
        VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (
            company,
            job_title,
            job_url,
            job_description,
            opportunity_type,
            deadline.astimezone(UTC).replace(microsecond=0).isoformat() if deadline else None,
            source,
        ),
    )
    return int(cur.lastrowid or 0)
