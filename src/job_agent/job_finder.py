"""Orchestre la recherche d'opportunités sur plusieurs sources et alimente la DB.

Pipeline :
1. Pour chaque source active (watch_sources + APIs si clés) : appelle search().
2. Dédoublonne par URL canonique.
3. Skip les URLs déjà connues en DB.
4. Détecte le type d'opportunité via LLM si la source ne le précise pas.
5. Si la source a free_funded_only=True : extrait funding et filtre.
6. Insère en DB avec status=pending.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from pydantic import TypeAdapter

from . import db, llm
from .logging_setup import get_logger
from .models import (
    Opportunity,
    OpportunityType,
    SearchQuery,
    WatchSource,
)
from .sources import build_from_api_config, build_from_watch_source

log = get_logger("job_finder")

_WATCH_LIST_ADAPTER: TypeAdapter[list[WatchSource]] = TypeAdapter(list[WatchSource])


def import_watch_list_from_json(conn: sqlite3.Connection, path: Path) -> int:
    """Import / upsert d'un fichier data/watch_sources.json dans la table."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("sources", data) if isinstance(data, dict) else data
    sources = _WATCH_LIST_ADAPTER.validate_python(items)
    with db.transaction(conn):
        for s in sources:
            db.upsert_watch_source(
                conn,
                name=s.name,
                url=s.url,
                method=s.method.value,
                opportunity_type=s.opportunity_type.value,
                refresh_hours=s.refresh_hours,
                free_funded_only=s.free_funded_only,
                enabled=s.enabled,
                notes=s.notes,
            )
    log.info("Watch list imported: %d sources", len(sources))
    return len(sources)


def canonical_url(url: str) -> str:
    """Normalise une URL pour la déduplication (strip trailing slash + tracking)."""
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme}://{p.netloc.lower()}{path}"


class JobFinder:
    """Orchestrateur de la recherche multi-source."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def search(self, query: SearchQuery, *, include_apis: bool = True) -> dict[str, int]:
        """Lance la recherche sur toutes les sources activées.

        Returns:
            dict[source_name, nb_opportunities_inserted]
        """
        per_source: dict[str, int] = {}
        opportunities: list[Opportunity] = []

        watch_rows = db.list_watch_sources(self.conn, only_enabled=True)
        for row in watch_rows:
            try:
                src = build_from_watch_source(row)
                found = src.search(query)
                per_source[row["name"]] = len(found)
                opportunities.extend(_attach_filter_flag(found, bool(row["free_funded_only"])))
                with db.transaction(self.conn):
                    db.mark_watch_source_run(self.conn, row["name"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Source %s failed: %s", row["name"], exc)
                per_source[row["name"]] = -1

        if include_apis:
            for api_src in build_from_api_config():
                try:
                    found = api_src.search(query)
                    per_source[api_src.name] = len(found)
                    opportunities.extend(_attach_filter_flag(found, False))
                except Exception as exc:  # noqa: BLE001
                    log.warning("API source %s failed: %s", api_src.name, exc)
                    per_source[api_src.name] = -1

        inserted = self._persist(opportunities, query)
        per_source["_inserted_total"] = inserted
        return per_source

    def _persist(
        self,
        opportunities: list[tuple[Opportunity, bool]],
        query: SearchQuery,
    ) -> int:
        """Insère les opportunités en DB après dédup, détection type et filtre funding."""
        seen: set[str] = set()
        inserted = 0
        for opp, must_filter_funding in opportunities:
            canon = canonical_url(opp.url)
            if canon in seen:
                continue
            seen.add(canon)
            if db.find_application_by_url(self.conn, opp.url) is not None:
                continue

            opp_type = opp.opportunity_type
            deadline: datetime | None = opp.deadline
            if opp.source.startswith(("rss:", "html:", "browser:")):
                try:
                    detection = llm.detect_opportunity_type(
                        opp.title, opp.organization, opp.description
                    )
                    opp_type = detection.opportunity_type
                    if detection.deadline_iso:
                        with contextlib.suppress(ValueError):
                            deadline = datetime.fromisoformat(detection.deadline_iso)
                except Exception as exc:  # noqa: BLE001
                    log.debug("detect_opportunity_type failed for %s: %s", opp.url, exc)

            if query.opportunity_types and opp_type not in query.opportunity_types:
                continue

            if must_filter_funding or query.free_only:
                try:
                    funding = llm.extract_funding(opp.title, opp.description)
                except Exception as exc:  # noqa: BLE001
                    log.debug("extract_funding failed: %s", exc)
                    continue
                if must_filter_funding and (
                    not funding.is_free_to_enter or not funding.is_funded
                ):
                    continue
                if query.free_only and not funding.is_free_to_enter:
                    continue
                if query.min_funding_eur and (
                    funding.funding_amount_eur is None
                    or funding.funding_amount_eur < query.min_funding_eur
                ):
                    continue

            with db.transaction(self.conn):
                db.insert_opportunity_application(
                    self.conn,
                    company=opp.organization,
                    job_title=opp.title,
                    job_url=opp.url,
                    job_description=opp.description,
                    opportunity_type=opp_type.value,
                    deadline=deadline,
                    source=opp.source,
                )
            inserted += 1
        return inserted


def _attach_filter_flag(
    opportunities: list[Opportunity], free_funded_only: bool
) -> list[tuple[Opportunity, bool]]:
    return [(o, free_funded_only) for o in opportunities]


def build_query_from_profile(
    *,
    keywords_override: list[str] | None = None,
    location_override: str | None = None,
    opportunity_types: list[OpportunityType] | None = None,
    max_results: int = 50,
    days: int = 14,
    free_only: bool = False,
    min_funding_eur: int = 0,
    profile_target_roles: list[str] | None = None,
    profile_skills: list[str] | None = None,
    profile_city: str | None = None,
) -> SearchQuery:
    """Construit une SearchQuery par défaut à partir du profil utilisateur."""
    keywords = list(keywords_override or [])
    if not keywords:
        keywords = list(profile_target_roles or [])[:3]
        if not keywords and profile_skills:
            keywords = list(profile_skills)[:3]
    return SearchQuery(
        keywords=keywords,
        location=location_override or profile_city,
        opportunity_types=opportunity_types or [],
        max_results_per_source=max_results,
        posted_within_days=days,
        free_only=free_only,
        min_funding_eur=min_funding_eur,
    )
