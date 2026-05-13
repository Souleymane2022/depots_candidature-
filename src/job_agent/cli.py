"""CLI typer : init, add, run, status, inbox, doctor."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import db, llm
from .agent import Agent, load_profile
from .auth_manager import AuthManager, Vault, root_domain, shared_chrome_profile_dir
from .config import (
    DEFAULT_INBOX_HOURS,
    DEFAULT_RATE_PER_HOUR,
    DEFAULT_SCORE_THRESHOLD,
    DISPLAY_TZ,
    Config,
    load,
)
from .email_pool import EmailPool
from .imap_reader import can_connect
from .job_finder import JobFinder, build_query_from_profile, import_watch_list_from_json
from .logging_setup import setup
from .models import (
    ApplicationStatus,
    AuthMethod,
    EmailStatus,
    OpportunityType,
    WatchSourceMethod,
)

app = typer.Typer(
    name="job-agent",
    help="Agent IA autonome de candidature automatique.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _bootstrap(verbose: bool) -> tuple[Config, sqlite3.Connection]:
    cfg = load()
    cfg.ensure_dirs()
    setup(cfg.logs_dir, verbose=verbose, console=console)
    conn = db.bootstrap(cfg.db_path)
    return cfg, conn


def _format_local(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


@app.command()
def init(
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Crée la DB, importe emails.json, et valide chaque email en se connectant en IMAP."""
    cfg, conn = _bootstrap(verbose)
    if not cfg.emails_path.exists():
        console.print(
            f"[red]Fichier emails introuvable : {cfg.emails_path}[/red]\n"
            f"Crée-le à partir de data/emails.example.json."
        )
        raise typer.Exit(1)

    pool = EmailPool(conn)
    n = pool.import_from_json(cfg.emails_path)
    console.print(f"[green]Imported {n} emails.[/green]")

    for account in pool.list_all():
        ok = can_connect(account)
        status_label = "[green]ok[/green]" if ok else "[red]invalid[/red]"
        console.print(f"  - {account.address} ... {status_label}")
        if not ok:
            pool.set_status(account.address, EmailStatus.INVALID)
    console.print("[green]Init terminé.[/green]")


@app.command()
def add(
    url: Annotated[str, typer.Argument(help="URL de l'offre d'emploi")],
    auto: Annotated[bool, typer.Option("--auto", help="Lance apply_to_job immédiatement")] = False,
    threshold: Annotated[int, typer.Option("--threshold", help="Score min")] = DEFAULT_SCORE_THRESHOLD,
    headless: Annotated[bool, typer.Option("--headless")] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Ajoute une offre au pipeline. Avec --auto, la traite immédiatement."""
    cfg, conn = _bootstrap(verbose)
    existing = db.find_application_by_url(conn, url)
    if existing is not None:
        console.print(
            f"[yellow]Déjà connue (app id={existing['id']}, status={existing['status']}).[/yellow]"
        )
        if not auto:
            raise typer.Exit(0)
    else:
        with db.transaction(conn):
            new_id = db.insert_application(
                conn,
                company="(à scraper)",
                job_title="(à scraper)",
                job_url=url,
                status=ApplicationStatus.PENDING,
            )
        console.print(f"[green]Ajoutée : app id={new_id}[/green]")

    if auto:
        profile = load_profile(cfg.profile_path)
        agent = Agent(cfg, conn, profile)
        agent.apply_to_job(url, threshold=threshold, headless=headless)


@app.command()
def run(
    max_count: Annotated[int | None, typer.Option("--max", help="Nb max d'offres à traiter")] = None,
    threshold: Annotated[int, typer.Option("--threshold")] = DEFAULT_SCORE_THRESHOLD,
    rate: Annotated[int, typer.Option("--rate", help="Candidatures/heure")] = DEFAULT_RATE_PER_HOUR,
    headless: Annotated[bool, typer.Option("--headless")] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Traite toutes les offres en status pending."""
    cfg, conn = _bootstrap(verbose)
    profile = load_profile(cfg.profile_path)
    agent = Agent(cfg, conn, profile)
    ids = agent.run_pending(
        max_count=max_count,
        threshold=threshold,
        rate_per_hour=rate,
        headless=headless,
    )
    console.print(f"[green]Traité {len(ids)} offre(s).[/green]")


@app.command()
def status(
    last: Annotated[int, typer.Option("--last", help="Nb d'entrées à afficher")] = 20,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Affiche un tableau des candidatures récentes."""
    cfg, conn = _bootstrap(verbose)
    rows = db.list_recent(conn, limit=last)
    if not rows:
        console.print("[yellow]Aucune candidature pour le moment.[/yellow]")
        return
    table = Table(title=f"Dernières {len(rows)} candidatures", show_lines=False)
    table.add_column("id", justify="right")
    table.add_column("status")
    table.add_column("score", justify="right")
    table.add_column("company")
    table.add_column("title")
    table.add_column("applied_at")
    table.add_column("email_used")
    for r in rows:
        color = _color_for_status(r["status"])
        table.add_row(
            str(r["id"]),
            f"[{color}]{r['status']}[/{color}]",
            str(r["score"]) if r["score"] is not None else "-",
            r["company"] or "-",
            (r["job_title"] or "-")[:50],
            _format_local(r["applied_at"]),
            r["email_used"] or "-",
        )
    console.print(table)


@app.command()
def inbox(
    hours: Annotated[int, typer.Option("--hours")] = DEFAULT_INBOX_HOURS,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Check IMAP sur toutes les boîtes, classifie, met à jour la DB."""
    cfg, conn = _bootstrap(verbose)
    profile = load_profile(cfg.profile_path)
    agent = Agent(cfg, conn, profile)
    n = agent.inbox_check(hours=hours)
    console.print(f"[green]{n} event(s) traité(s).[/green]")


@app.command()
def doctor(
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Vérifie .env, profile.json, emails.json, CV, API Claude, connexions IMAP."""
    cfg, conn = _bootstrap(verbose)
    issues: list[str] = []

    if not cfg.anthropic_api_key:
        issues.append("ANTHROPIC_API_KEY manquante dans .env")
    if not cfg.profile_path.exists():
        issues.append(f"profile introuvable : {cfg.profile_path}")
    if not cfg.emails_path.exists():
        issues.append(f"emails introuvable : {cfg.emails_path}")

    profile = None
    if cfg.profile_path.exists():
        try:
            profile = load_profile(cfg.profile_path)
            console.print(
                f"[green]profile.json OK[/green] ({profile.first_name} {profile.last_name})"
            )
            cv_p = Path(profile.cv_path)
            cv_path = cv_p if cv_p.is_absolute() else cfg.project_root / cv_p
            if not cv_path.exists():
                issues.append(f"CV introuvable : {cv_path}")
            else:
                console.print(f"[green]CV OK[/green] ({cv_path})")
        except Exception as exc:  # noqa: BLE001
            issues.append(f"profile.json invalide : {exc}")

    if cfg.emails_path.exists():
        pool = EmailPool(conn)
        try:
            pool.import_from_json(cfg.emails_path)
        except Exception as exc:  # noqa: BLE001
            issues.append(f"emails.json invalide : {exc}")

    if cfg.anthropic_api_key:
        try:
            client = llm.get_client()
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=8,
                messages=[{"role": "user", "content": "ping"}],
            )
            console.print("[green]API Claude OK[/green]")
        except Exception as exc:  # noqa: BLE001
            issues.append(f"API Claude KO : {exc}")

    pool = EmailPool(conn)
    for account in pool.list_all():
        ok = can_connect(account)
        if ok:
            console.print(f"[green]IMAP OK[/green] {account.address}")
        else:
            issues.append(f"IMAP KO : {account.address}")

    if issues:
        console.print(
            Panel.fit(
                "\n".join(f"- {i}" for i in issues),
                title="[red]Problèmes détectés[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(1)
    console.print("[bold green]All checks passed.[/bold green]")


# ---------------------------------------------------------------------------
# search : recherche multi-source
# ---------------------------------------------------------------------------


@app.command()
def search(
    keywords: Annotated[
        str | None, typer.Option("--keywords", help="Mots-clés (sépare par virgules). Sinon, profil.")
    ] = None,
    location: Annotated[str | None, typer.Option("--location")] = None,
    types: Annotated[
        str | None,
        typer.Option("--types", help="Filtre par types (csv): job,contest,event,cfp"),
    ] = None,
    max_per_source: Annotated[int, typer.Option("--max", help="Max par source")] = 50,
    days: Annotated[int, typer.Option("--days", help="Postées dans les N derniers jours")] = 14,
    free_only: Annotated[bool, typer.Option("--free-only", help="Filtre opportunités gratuites")] = False,
    min_funding: Annotated[int, typer.Option("--min-funding", help="Dotation minimum EUR")] = 0,
    no_apis: Annotated[bool, typer.Option("--no-apis", help="Exclut Adzuna/France Travail")] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Lance la recherche d'opportunités sur toutes les sources actives."""
    cfg, conn = _bootstrap(verbose)
    profile = load_profile(cfg.profile_path) if cfg.profile_path.exists() else None

    kw_list = [k.strip() for k in keywords.split(",")] if keywords else None
    types_list: list[OpportunityType] | None = None
    if types:
        types_list = [OpportunityType(t.strip()) for t in types.split(",") if t.strip()]

    query = build_query_from_profile(
        keywords_override=kw_list,
        location_override=location,
        opportunity_types=types_list,
        max_results=max_per_source,
        days=days,
        free_only=free_only,
        min_funding_eur=min_funding,
        profile_target_roles=profile.target_roles if profile else None,
        profile_skills=profile.skills if profile else None,
        profile_city=profile.city if profile else None,
    )

    if not query.keywords and not location:
        console.print(
            "[yellow]Aucun keyword et pas de profil : la recherche risque d'être pauvre.[/yellow]"
        )

    console.print(f"[cyan]Recherche : keywords={query.keywords} location={query.location}[/cyan]")
    finder = JobFinder(conn)
    stats = finder.search(query, include_apis=not no_apis)

    table = Table(title="Résultats par source")
    table.add_column("source")
    table.add_column("retrouvé", justify="right")
    for name, n in stats.items():
        if name == "_inserted_total":
            continue
        label = "[red]erreur[/red]" if n < 0 else str(n)
        table.add_row(name, label)
    console.print(table)
    inserted = stats.get("_inserted_total", 0)
    console.print(f"[green]{inserted} nouvelle(s) opportunité(s) ajoutée(s) en pending.[/green]")


# ---------------------------------------------------------------------------
# watch : gestion des sources veillées
# ---------------------------------------------------------------------------

watch_app = typer.Typer(help="Gère les sources d'opportunités à veiller.", no_args_is_help=True)
app.add_typer(watch_app, name="watch")


@watch_app.command("add")
def watch_add(
    url: Annotated[str, typer.Argument(help="URL de la page / flux")],
    name: Annotated[str | None, typer.Option("--name")] = None,
    method: Annotated[
        WatchSourceMethod, typer.Option("--method", help="rss|html_scrape|browser_use")
    ] = WatchSourceMethod.HTML_SCRAPE,
    type_: Annotated[
        OpportunityType,
        typer.Option("--type", help="job|contest|event|cfp"),
    ] = OpportunityType.JOB,
    every: Annotated[int, typer.Option("--every", help="Refresh toutes les N heures")] = 24,
    free_only: Annotated[bool, typer.Option("--free-only")] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Ajoute une source à veiller (RSS, HTML, ou browser-use)."""
    _, conn = _bootstrap(verbose)
    derived_name = name or root_domain(url) or url
    with db.transaction(conn):
        db.upsert_watch_source(
            conn,
            name=derived_name,
            url=url,
            method=method.value,
            opportunity_type=type_.value,
            refresh_hours=every,
            free_funded_only=free_only,
            enabled=True,
        )
    console.print(f"[green]Source ajoutée :[/green] {derived_name} ({method.value})")


@watch_app.command("list")
def watch_list_cmd(
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Liste les sources veillées."""
    _, conn = _bootstrap(verbose)
    rows = db.list_watch_sources(conn)
    if not rows:
        console.print("[yellow]Aucune source. Ajoute avec : job-agent watch add <url>[/yellow]")
        return
    table = Table(title="Sources veillées")
    table.add_column("name")
    table.add_column("method")
    table.add_column("type")
    table.add_column("free?")
    table.add_column("every")
    table.add_column("enabled")
    table.add_column("last_run")
    for r in rows:
        table.add_row(
            r["name"],
            r["method"],
            r["opportunity_type"],
            "✓" if r["free_funded_only"] else "",
            f"{r['refresh_hours']}h",
            "[green]on[/green]" if r["enabled"] else "[red]off[/red]",
            _format_local(r["last_run_at"]),
        )
    console.print(table)


@watch_app.command("enable")
def watch_enable(
    name: Annotated[str, typer.Argument()],
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    _, conn = _bootstrap(verbose)
    with db.transaction(conn):
        n = db.set_watch_source_enabled(conn, name, True)
    console.print(f"[green]Enabled[/green] {n} source(s).")


@watch_app.command("disable")
def watch_disable(
    name: Annotated[str, typer.Argument()],
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    _, conn = _bootstrap(verbose)
    with db.transaction(conn):
        n = db.set_watch_source_enabled(conn, name, False)
    console.print(f"[yellow]Disabled[/yellow] {n} source(s).")


@watch_app.command("remove")
def watch_remove(
    name: Annotated[str, typer.Argument()],
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    _, conn = _bootstrap(verbose)
    with db.transaction(conn):
        n = db.delete_watch_source(conn, name)
    console.print(f"[yellow]Removed[/yellow] {n} source(s).")


@watch_app.command("import")
def watch_import(
    path: Annotated[Path, typer.Argument(help="Chemin vers watch_sources.json")],
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Importe (upsert) un fichier JSON de sources veillées."""
    _, conn = _bootstrap(verbose)
    n = import_watch_list_from_json(conn, path)
    console.print(f"[green]Imported {n} watch sources from {path}[/green]")


# ---------------------------------------------------------------------------
# vault : credentials chiffrés pour les sites
# ---------------------------------------------------------------------------

vault_app = typer.Typer(help="Gère le vault de credentials (Fernet).", no_args_is_help=True)
app.add_typer(vault_app, name="vault")


@vault_app.command("add")
def vault_add(
    domain: Annotated[str, typer.Argument(help="ex: greenhouse.io")],
    username: Annotated[str, typer.Option("--username", prompt=True)],
    password: Annotated[
        str, typer.Option("--password", prompt=True, hide_input=True, confirmation_prompt=False)
    ],
    auth: Annotated[
        AuthMethod, typer.Option("--auth", help="stored|shared_chrome")
    ] = AuthMethod.STORED,
    notes: Annotated[str, typer.Option("--notes")] = "",
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Ajoute/maj un credential chiffré pour un domaine."""
    _, conn = _bootstrap(verbose)
    vault = Vault.from_env_or_prompt()
    am = AuthManager(conn, vault)
    am.add_credential(
        site_domain=domain,
        username=username,
        password=password,
        auth_method=auth,
        notes=notes,
    )
    console.print(f"[green]Stored credential for {domain}.[/green]")


@vault_app.command("list")
def vault_list(
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Liste les domaines avec credentials (sans révéler les mots de passe)."""
    _, conn = _bootstrap(verbose)
    rows = db.list_site_credentials(conn)
    if not rows:
        console.print("[yellow]Aucun credential.[/yellow]")
        return
    table = Table(title="Credentials")
    table.add_column("domain")
    table.add_column("auth")
    table.add_column("username")
    table.add_column("has_password")
    for r in rows:
        table.add_row(
            r["site_domain"],
            r["auth_method"],
            r["username"] or "-",
            "✓" if r["password_encrypted"] else "",
        )
    console.print(table)


@vault_app.command("remove")
def vault_remove(
    domain: Annotated[str, typer.Argument()],
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    _, conn = _bootstrap(verbose)
    with db.transaction(conn):
        n = db.delete_site_credential(conn, domain)
    console.print(f"[yellow]Removed[/yellow] {n} credential(s).")


# ---------------------------------------------------------------------------
# login : ouvre Chrome shared profile pour se logger manuellement
# ---------------------------------------------------------------------------


@app.command()
def login(
    domain: Annotated[str, typer.Argument(help="Domaine, ex: linkedin.com")],
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Lance Chrome (profil partagé) pour que tu te connectes manuellement.

    Ta session est sauvegardée dans data/.browser_profile/ et réutilisée
    automatiquement par les soumissions ultérieures.
    """
    cfg, conn = _bootstrap(verbose)
    profile_dir = shared_chrome_profile_dir(cfg.project_root)
    console.print(
        Panel.fit(
            f"Chrome va s'ouvrir sur https://{domain}\n\n"
            f"Profil partagé : {profile_dir}\n\n"
            "Connecte-toi normalement, navigue, vérifie que c'est OK.\n"
            "Quand c'est bon, ferme la fenêtre — la session sera sauvegardée.",
            title="Login manuel",
            border_style="cyan",
        )
    )
    import asyncio

    asyncio.run(_open_login_browser(domain, profile_dir))
    with db.transaction(conn):
        db.record_auth_event(
            conn,
            site_domain=domain,
            event_type="session_capture",
            success=True,
            notes=f"profile_dir={profile_dir}",
        )
    console.print("[green]Session capturée.[/green]")


async def _open_login_browser(domain: str, profile_dir: Path) -> None:
    import contextlib

    from patchright.async_api import async_playwright

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            locale="fr-FR",
        )
        try:
            page = await ctx.new_page()
            await page.goto(f"https://{domain}", wait_until="domcontentloaded", timeout=60000)
            with contextlib.suppress(Exception):
                await page.wait_for_event("close", timeout=600000)
        finally:
            await ctx.close()


def _color_for_status(s: str) -> str:
    return {
        "pending": "yellow",
        "scored": "cyan",
        "rejected": "red",
        "submitted": "green",
        "confirmed": "bold green",
        "failed": "red",
        "interview": "magenta",
    }.get(s, "white")


def main() -> None:
    try:
        app()
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        console.print("[yellow]Interrompu.[/yellow]")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Erreur : {exc}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
