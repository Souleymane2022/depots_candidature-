"""Orchestration : apply_to_job (boucle principale), run_pending, inbox_check."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from . import browser, cv_adapter, db, llm
from .config import (
    DEFAULT_CONFIRMATION_POLL_S,
    DEFAULT_CONFIRMATION_TIMEOUT_S,
    DEFAULT_INBOX_HOURS,
    DEFAULT_RATE_PER_HOUR,
    DEFAULT_SCORE_THRESHOLD,
    Config,
)
from .email_pool import EmailPool
from .imap_reader import wait_for_confirmation
from .logging_setup import get_logger
from .models import (
    ApplicationStatus,
    EmailAccount,
    LetterLanguageMode,
    Profile,
)

log = get_logger("agent")
_PROFILE_ADAPTER: TypeAdapter[Profile] = TypeAdapter(Profile)


def load_profile(path: Path) -> Profile:
    """Charge et valide data/profile.json."""
    return _PROFILE_ADAPTER.validate_json(Path(path).read_text(encoding="utf-8"))


class Agent:
    """Orchestrateur. Tient l'état partagé (conn DB, config, profile, pool, cv_text)."""

    def __init__(self, config: Config, conn: sqlite3.Connection, profile: Profile) -> None:
        self.config = config
        self.conn = conn
        self.profile = profile
        self.pool = EmailPool(conn)
        cv_path = self._resolve_cv_path()
        self.cv_text = (
            cv_adapter.extract_cv_text(cv_path, cache_dir=config.cache_dir)
            if cv_path.exists()
            else ""
        )

    def _resolve_cv_path(self) -> Path:
        p = Path(self.profile.cv_path)
        return p if p.is_absolute() else (self.config.project_root / p)

    def apply_to_job(
        self,
        job_url: str,
        *,
        threshold: int = DEFAULT_SCORE_THRESHOLD,
        headless: bool = False,
    ) -> int:
        """Pipeline complet pour une URL. Renvoie l'application_id (existant ou créé)."""
        # 1. Doublon ?
        existing = db.find_application_by_url(self.conn, job_url)
        if existing is not None and existing["status"] in (
            ApplicationStatus.SUBMITTED.value,
            ApplicationStatus.CONFIRMED.value,
            ApplicationStatus.INTERVIEW.value,
        ):
            log.info("Skip duplicate: app=%d already %s", existing["id"], existing["status"])
            return int(existing["id"])

        # 2. Scrape l'offre
        offer = asyncio.run(browser.scrape_offer(job_url, headless=True))

        # 3. Insert / récupère l'application
        if existing is None:
            app_id = db.insert_application(
                self.conn,
                company=offer.company,
                job_title=offer.title,
                job_url=job_url,
                job_description=offer.description,
                status=ApplicationStatus.PENDING,
            )
        else:
            app_id = int(existing["id"])
            with db.transaction(self.conn):
                db.update_application(
                    self.conn,
                    app_id,
                    company=offer.company,
                    job_title=offer.title,
                    job_description=offer.description,
                )

        # 4. Scoring
        score_result = llm.score_offer(self.profile, offer, cv_text=self.cv_text)
        with db.transaction(self.conn):
            db.update_application(
                self.conn,
                app_id,
                score=score_result.score,
                score_reason=score_result.reason,
                status=ApplicationStatus.SCORED.value,
            )
        log.info(
            "Scored app=%d score=%d (lang=%s)",
            app_id,
            score_result.score,
            score_result.language_detected,
        )

        if score_result.score < threshold:
            with db.transaction(self.conn):
                db.update_application(
                    self.conn, app_id, status=ApplicationStatus.REJECTED.value
                )
            log.info("Rejected app=%d (score %d < %d)", app_id, score_result.score, threshold)
            return app_id

        # 5. Email pool
        email = self.pool.pick_email_for_company(offer.company)
        if email is None:
            with db.transaction(self.conn):
                db.update_application(
                    self.conn,
                    app_id,
                    status=ApplicationStatus.FAILED.value,
                    notes="email pool exhausted for company",
                )
            return app_id

        # 6. Lettre de motivation (auto-détection langue)
        letter = llm.write_cover_letter(
            self.profile, offer, language_mode=LetterLanguageMode.AUTO, cv_text=self.cv_text
        )
        with db.transaction(self.conn):
            db.update_application(self.conn, app_id, cover_letter=letter.text)

        # 7. Artefacts (lettre + CV de base copié)
        folder = cv_adapter.save_artifacts(
            generated_dir=self.config.generated_dir,
            application_id=app_id,
            cover_letter_text=letter.text,
            cv_path=self._resolve_cv_path(),
        )

        # 8. Soumission browser
        submission = browser.submit_application(
            profile=self.profile,
            email=email,
            cover_letter=letter.text,
            cv_path=folder / "cv.pdf",
            offer=offer,
            headless=headless,
        )

        applied_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        if submission.submitted:
            with db.transaction(self.conn):
                db.update_application(
                    self.conn,
                    app_id,
                    email_used=email.address,
                    applied_at=applied_at,
                    status=ApplicationStatus.SUBMITTED.value,
                    notes=submission.evidence,
                )
                db.mark_email_used(self.conn, email.address)
            log.info("Submitted app=%d via %s", app_id, email.address)

            # 9. Confirmation IMAP en arrière-plan (thread daemon)
            t = threading.Thread(
                target=self._wait_confirmation_thread,
                args=(app_id, email, datetime.now(UTC)),
                daemon=True,
                name=f"confirm-app-{app_id}",
            )
            t.start()
        else:
            with db.transaction(self.conn):
                db.update_application(
                    self.conn,
                    app_id,
                    status=ApplicationStatus.FAILED.value,
                    notes=submission.error or submission.evidence,
                )
            log.warning("Submission failed app=%d: %s", app_id, submission.error)

        return app_id

    def _wait_confirmation_thread(
        self, app_id: int, email: EmailAccount, since: datetime
    ) -> None:
        """Thread daemon : poll IMAP 5min, met à jour le status si confirmation reçue."""
        try:
            url = wait_for_confirmation(
                email,
                since,
                timeout=DEFAULT_CONFIRMATION_TIMEOUT_S,
                poll=DEFAULT_CONFIRMATION_POLL_S,
            )
            if url is None:
                log.info("No confirmation for app=%d within timeout", app_id)
                return
            # Visite l'URL et clique sur le bouton de validation (best-effort)
            ok = asyncio.run(_visit_confirmation_url(url))
            if ok:
                with db.transaction(self.conn):
                    db.update_application(
                        self.conn,
                        app_id,
                        status=ApplicationStatus.CONFIRMED.value,
                        response_received_at=datetime.now(UTC)
                        .replace(microsecond=0)
                        .isoformat(),
                    )
                log.info("Confirmed app=%d", app_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Confirmation thread failed app=%d: %s", app_id, exc)

    def run_pending(
        self,
        *,
        max_count: int | None = None,
        threshold: int = DEFAULT_SCORE_THRESHOLD,
        rate_per_hour: int = DEFAULT_RATE_PER_HOUR,
        headless: bool = False,
        sleep_fn: object = time.sleep,
    ) -> list[int]:
        """Traite toutes les offres pending, jusqu'à max_count, en respectant le rate."""
        delay = max(1.0, 3600.0 / max(1, rate_per_hour))
        pending = db.list_pending(self.conn, limit=max_count)
        processed: list[int] = []
        for i, row in enumerate(pending):
            try:
                app_id = self.apply_to_job(
                    row["job_url"], threshold=threshold, headless=headless
                )
                processed.append(app_id)
            except Exception as exc:  # noqa: BLE001
                log.exception("apply_to_job failed for %s: %s", row["job_url"], exc)
                with db.transaction(self.conn):
                    db.update_application(
                        self.conn,
                        int(row["id"]),
                        status=ApplicationStatus.FAILED.value,
                        notes=f"unhandled: {exc!r}",
                    )
            if i < len(pending) - 1:
                sleep_fn(delay)  # type: ignore[operator]
        return processed

    def inbox_check(self, hours: int = DEFAULT_INBOX_HOURS) -> int:
        """Pour chaque email actif : classifie les mails récents, insère en DB, met à jour status."""
        from .imap_reader import classify_recent

        n_events = 0
        for account in self.pool.list_active():
            events = classify_recent(account, hours=hours)
            for evt in events:
                # Tente de matcher l'event à une application par domaine du sender
                app_id = _match_application(self.conn, evt.sender)
                with db.transaction(self.conn):
                    db.record_inbox_event(
                        self.conn,
                        email=account.address,
                        application_id=app_id,
                        received_at=evt.received_at,
                        subject=evt.subject,
                        sender=evt.sender,
                        event_type=evt.event_type,
                        raw_snippet=evt.raw_snippet,
                    )
                    if app_id is not None:
                        new_status = _status_for_event(evt.event_type)
                        if new_status is not None:
                            db.update_application(
                                self.conn,
                                app_id,
                                status=new_status.value,
                                response_received_at=evt.received_at.astimezone(UTC)
                                .replace(microsecond=0)
                                .isoformat(),
                            )
                n_events += 1
        return n_events


def _match_application(conn: sqlite3.Connection, sender: str) -> int | None:
    """Best-effort : matche un email entrant à une application via le domaine du sender."""
    if "@" not in sender:
        return None
    domain = sender.split("@", 1)[1].lower().strip(">")
    root = domain.split(".")[0]
    row = conn.execute(
        """
        SELECT id FROM applications
        WHERE LOWER(company) LIKE ?
        ORDER BY id DESC LIMIT 1
        """,
        (f"%{root}%",),
    ).fetchone()
    return int(row["id"]) if row else None


def _status_for_event(event_type: object) -> ApplicationStatus | None:
    from .models import EmailEventType

    mapping = {
        EmailEventType.CONFIRMATION: ApplicationStatus.CONFIRMED,
        EmailEventType.REJECTION: ApplicationStatus.REJECTED,
        EmailEventType.INTERVIEW_INVITE: ApplicationStatus.INTERVIEW,
    }
    return mapping.get(event_type)  # type: ignore[arg-type]


async def _visit_confirmation_url(url: str) -> bool:
    """Visite l'URL de confirmation et clique sur un éventuel bouton de validation."""
    from patchright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        ctx = await b.new_context()
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            for selector in (
                "button:has-text('Confirmer')",
                "button:has-text('Confirm')",
                "button:has-text('Valider')",
                "button:has-text('Verify')",
                "a:has-text('Confirmer')",
                "a:has-text('Confirm')",
            ):
                try:
                    btn = page.locator(selector).first
                    if await btn.count() > 0:
                        await btn.click(timeout=3000)
                        break
                except Exception:  # noqa: BLE001
                    continue
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("visit_confirmation_url failed: %s", exc)
            return False
        finally:
            await ctx.close()
            await b.close()
