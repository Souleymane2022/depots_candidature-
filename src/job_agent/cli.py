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
from .logging_setup import setup
from .models import ApplicationStatus, EmailStatus

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
