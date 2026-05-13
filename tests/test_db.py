"""Tests du module db : schéma, contraintes, transactions."""

from __future__ import annotations

import sqlite3

import pytest

from job_agent import db
from job_agent.models import ApplicationStatus, EmailEventType, EmailStatus


def test_bootstrap_is_idempotent(tmp_path):
    p = tmp_path / "agent.db"
    db.bootstrap(p).close()
    db.bootstrap(p).close()
    conn = db.bootstrap(p)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"emails", "applications", "email_assignments", "inbox_events"} <= tables


def test_upsert_email_preserves_last_used_at(temp_db, sample_emails):
    from datetime import datetime
    db.upsert_email(temp_db, sample_emails[0])
    db.mark_email_used(temp_db, sample_emails[0]["address"])
    before = temp_db.execute(
        "SELECT last_used_at FROM emails WHERE address = ?", (sample_emails[0]["address"],)
    ).fetchone()["last_used_at"]
    assert before is not None

    updated = {**sample_emails[0], "display_name": "Alice 2"}
    db.upsert_email(temp_db, updated)
    after = temp_db.execute(
        "SELECT last_used_at, display_name FROM emails WHERE address = ?",
        (sample_emails[0]["address"],),
    ).fetchone()
    assert after["last_used_at"] == before
    assert after["display_name"] == "Alice 2"
    datetime.fromisoformat(before)


def test_unique_job_url(temp_db):
    db.insert_application(temp_db, company="A", job_title="T1", job_url="https://x/1")
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_application(temp_db, company="B", job_title="T2", job_url="https://x/1")


def test_email_assignments_pk_composite(temp_db, sample_emails):
    for e in sample_emails:
        db.upsert_email(temp_db, e)
    db.record_email_assignment(temp_db, None, "alice@example.com", "Acme")
    db.record_email_assignment(temp_db, None, "alice@example.com", "Acme")
    n = temp_db.execute(
        "SELECT COUNT(*) FROM email_assignments WHERE email=? AND company=?",
        ("alice@example.com", "Acme"),
    ).fetchone()[0]
    assert n == 1


def test_update_application_with_status_enum(temp_db):
    app_id = db.insert_application(temp_db, company="A", job_title="T", job_url="https://x/1")
    db.update_application(temp_db, app_id, status=ApplicationStatus.SCORED, score=80)
    row = temp_db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    assert row["status"] == "scored"
    assert row["score"] == 80


def test_pick_least_recent_unused_email_orders_correctly(temp_db, sample_emails):
    for e in sample_emails:
        db.upsert_email(temp_db, e)
    # Alice used recently, Bob never used, Carol used long ago
    temp_db.execute(
        "UPDATE emails SET last_used_at = '2099-01-01T00:00:00+00:00' WHERE address = 'alice@example.com'"
    )
    temp_db.execute(
        "UPDATE emails SET last_used_at = '2000-01-01T00:00:00+00:00' WHERE address = 'carol@example.com'"
    )
    picked = db.pick_least_recent_unused_email(temp_db, "NewCo")
    # Bob has NULL last_used_at -> goes first
    assert picked["address"] == "bob@example.com"


def test_record_inbox_event(temp_db, sample_emails):
    from datetime import UTC, datetime
    db.upsert_email(temp_db, sample_emails[0])
    app_id = db.insert_application(temp_db, company="Acme", job_title="T", job_url="https://x/1")
    db.record_inbox_event(
        temp_db,
        email=sample_emails[0]["address"],
        application_id=app_id,
        received_at=datetime(2026, 5, 13, 12, 0, tzinfo=UTC),
        subject="Confirm your application",
        sender="noreply@acme.com",
        event_type=EmailEventType.CONFIRMATION,
        raw_snippet="please click here",
    )
    row = temp_db.execute("SELECT * FROM inbox_events").fetchone()
    assert row["event_type"] == "confirmation"
    assert row["application_id"] == app_id


def test_set_email_status(temp_db, sample_emails):
    db.upsert_email(temp_db, sample_emails[0])
    db.set_email_status(temp_db, sample_emails[0]["address"], EmailStatus.INVALID)
    row = temp_db.execute(
        "SELECT status FROM emails WHERE address=?", (sample_emails[0]["address"],)
    ).fetchone()
    assert row["status"] == "invalid"


def test_transaction_rollback_on_error(temp_db):
    initial = temp_db.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError), db.transaction(temp_db):
        db.insert_application(temp_db, company="A", job_title="T", job_url="https://x/1")
        db.insert_application(temp_db, company="B", job_title="T", job_url="https://x/1")
    final = temp_db.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    assert final == initial


def test_list_pending_and_recent(temp_db):
    for i in range(3):
        db.insert_application(temp_db, company=f"C{i}", job_title="T", job_url=f"https://x/{i}")
    pending = db.list_pending(temp_db)
    assert len(pending) == 3
    recent = db.list_recent(temp_db, limit=2)
    assert len(recent) == 2
    assert recent[0]["id"] > recent[1]["id"]
