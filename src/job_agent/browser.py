"""Pilotage du navigateur : patchright (stealth) + browser-use (LLM agent).

Choix de design :
- patchright fournit le Chromium stealth + le BrowserContext.
- browser-use s'attache à ce contexte via son API.
- User-agent et viewport randomisés pour limiter le fingerprinting.
- Délais humains entre actions (1.5-4.0s).
- CAPTCHA : pause interactive via input(), pas de service tiers.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from .logging_setup import get_logger
from .models import EmailAccount, JobOffer, Profile, SubmissionResult
from .prompts import render

log = get_logger("browser")
_console = Console()

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

_CAPTCHA_PATTERNS = (
    "recaptcha",
    "hcaptcha",
    "cf-challenge",
    "captcha",
    "are you human",
    "verify you are human",
)

_SUCCESS_URL_RE = re.compile(
    r"(thank|success|confirm|submitted|merci|envoye|envoyée|received|complete)",
    re.IGNORECASE,
)


def _random_ua() -> str:
    return random.choice(_USER_AGENTS)


def _random_viewport() -> dict[str, int]:
    return {
        "width": random.randint(1280, 1920),
        "height": random.randint(720, 1080),
    }


def _human_delay(headless: bool) -> None:
    if headless:
        return
    time.sleep(random.uniform(1.5, 4.0))


def _captcha_in_text(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _CAPTCHA_PATTERNS)


def _pause_for_human(reason: str) -> None:
    """Affiche une alerte rich et attend l'appui sur Entrée."""
    _console.print(
        Panel.fit(
            f"[bold yellow]{reason}[/bold yellow]\n\n"
            "Résous l'étape manuellement dans la fenêtre du navigateur,\n"
            "puis reviens ici et appuie sur [bold]Entrée[/bold] pour continuer.",
            border_style="yellow",
            title="Intervention humaine requise",
        )
    )
    try:
        input()
    except EOFError:
        log.warning("stdin closed, cannot pause for human; continuing")


async def _submit_async(
    *,
    profile: Profile,
    email: EmailAccount,
    cover_letter: str,
    cv_path: Path,
    offer: JobOffer,
    headless: bool,
) -> SubmissionResult:
    """Implémentation asynchrone : patchright + browser-use."""
    from patchright.async_api import async_playwright

    try:
        from browser_use import Agent as BUAgent
        from browser_use import Browser as BUBrowser
    except ImportError as exc:
        return SubmissionResult(
            submitted=False,
            evidence="",
            error=f"browser-use non installé : {exc}",
        )

    task = render(
        "browser_task",
        job_url=offer.url,
        company=offer.company,
        job_title=offer.title,
        profile_json=profile.model_dump_json(),
        email_address=email.address,
        cover_letter=cover_letter,
        cv_path=str(cv_path),
        years_experience=profile.years_experience,
        current_title=profile.current_title,
        city=profile.city,
        country=profile.country,
        linkedin=str(profile.linkedin or ""),
        github=str(profile.github or ""),
        remote_pref=profile.preferences.remote.value,
        salary_min_eur=profile.preferences.salary_min_eur,
    )

    ua = _random_ua()
    viewport = _random_viewport()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=ua,
            viewport=viewport,
            locale="fr-FR",
        )
        page = await context.new_page()
        log.info("Launching submission: %s @ %s", offer.title, offer.company)

        try:
            await page.goto(offer.url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            await context.close()
            await browser.close()
            return SubmissionResult(submitted=False, evidence="", error=f"goto failed: {exc}")

        if not headless:
            _human_delay(headless)

        try:
            body_text = await page.content()
            if _captcha_in_text(body_text):
                _pause_for_human("CAPTCHA détecté avant soumission")
        except Exception as exc:  # noqa: BLE001
            log.debug("captcha pre-check failed: %s", exc)

        bu_browser = BUBrowser(playwright_context=context) if _accepts_kw(BUBrowser, "playwright_context") else None
        try:
            agent = BUAgent(task=task, browser=bu_browser) if bu_browser else BUAgent(task=task)
            history = await agent.run()
        except Exception as exc:  # noqa: BLE001
            await context.close()
            await browser.close()
            return SubmissionResult(
                submitted=False, evidence="", error=f"browser-use error: {exc}"
            )

        final_url = page.url
        try:
            visible_text = await page.inner_text("body")
        except Exception:  # noqa: BLE001
            visible_text = ""

        evidence = f"final_url={final_url}"
        result = _judge_success(history, final_url, visible_text)
        evidence += f" | {result.evidence}"

        if not headless:
            _human_delay(headless)

        await context.close()
        await browser.close()
        return SubmissionResult(
            submitted=result.submitted, evidence=evidence, error=result.error
        )


def _accepts_kw(cls: type, kwarg: str) -> bool:
    """Détecte si un constructeur accepte un kwarg donné (compatibilité versions browser-use)."""
    import inspect

    try:
        sig = inspect.signature(cls.__init__)
        return kwarg in sig.parameters
    except (TypeError, ValueError):
        return False


def _judge_success(history: object, final_url: str, visible_text: str) -> SubmissionResult:
    """Détermine si la soumission a réussi à partir des signaux disponibles."""
    history_text = ""
    final = getattr(history, "final_result", None)
    if callable(final):
        try:
            history_text = str(final()) or ""
        except Exception:  # noqa: BLE001
            history_text = ""
    if not history_text:
        history_text = str(history)[:2000]

    if "CAPTCHA_DETECTED" in history_text.upper():
        return SubmissionResult(
            submitted=False, evidence="agent reported CAPTCHA", error="captcha"
        )

    if _SUCCESS_URL_RE.search(final_url):
        return SubmissionResult(submitted=True, evidence="success url pattern")

    low = visible_text.lower()
    confirmations = (
        "merci pour votre candidature",
        "candidature envoyée",
        "candidature reçue",
        "thank you for applying",
        "thanks for applying",
        "your application has been received",
        "application submitted",
        "we have received your application",
    )
    for marker in confirmations:
        if marker in low:
            return SubmissionResult(submitted=True, evidence=f"visible: {marker!r}")

    if any(kw in history_text.lower() for kw in ("submitted", "soumis", "envoye", "envoyée")):
        return SubmissionResult(
            submitted=True, evidence="agent self-reported submission"
        )

    return SubmissionResult(
        submitted=False,
        evidence="no success signal detected",
        error="success_not_detected",
    )


def submit_application(
    *,
    profile: Profile,
    email: EmailAccount,
    cover_letter: str,
    cv_path: Path,
    offer: JobOffer,
    headless: bool = False,
) -> SubmissionResult:
    """Point d'entrée synchrone : lance la coroutine asynchrone et renvoie le résultat."""
    try:
        return asyncio.run(
            _submit_async(
                profile=profile,
                email=email,
                cover_letter=cover_letter,
                cv_path=cv_path,
                offer=offer,
                headless=headless,
            )
        )
    except RuntimeError as exc:
        if "event loop is already running" in str(exc).lower():
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(
                _submit_async(
                    profile=profile,
                    email=email,
                    cover_letter=cover_letter,
                    cv_path=cv_path,
                    offer=offer,
                    headless=headless,
                )
            )
        raise


async def scrape_offer(url: str, headless: bool = True) -> JobOffer:
    """Scrape une page d'offre : récupère titre, entreprise et description (best-effort)."""
    from patchright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=_random_ua(),
            viewport=_random_viewport(),
            locale="fr-FR",
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            title = (await page.title()) or ""
            try:
                h1 = await page.inner_text("h1", timeout=3000)
            except Exception:  # noqa: BLE001
                h1 = ""
            body_text = await page.inner_text("body")
            company = _guess_company(title, body_text, url)
            job_title = (h1 or title).strip()
            return JobOffer(
                url=url,
                title=job_title or "(titre inconnu)",
                company=company,
                description=body_text[:8000],
            )
        finally:
            await context.close()
            await browser.close()


def _guess_company(title: str, body_text: str, url: str) -> str:
    """Heuristique simple : 'Titre - Entreprise' / 'Titre @ Entreprise' / nom de domaine."""
    for sep in (" - ", " | ", " @ ", " chez "):
        if sep in title:
            return title.split(sep, 1)[1].strip()
    for sep in (" - ", " | ", " @ ", " chez "):
        if sep in body_text[:500]:
            return body_text[:500].split(sep, 1)[1].splitlines()[0].strip()
    domain = url.split("//", 1)[-1].split("/", 1)[0]
    return domain.replace("www.", "").split(".")[0].capitalize()


__all__ = ["submit_application", "scrape_offer"]
