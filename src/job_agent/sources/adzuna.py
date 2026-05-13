"""Source Adzuna : recherche d'offres d'emploi via API REST.

Doc : https://developer.adzuna.com/overview
Clés requises : ADZUNA_APP_ID + ADZUNA_APP_KEY (gratuit, 250 req/mois).
"""

from __future__ import annotations

import os

import httpx

from ..logging_setup import get_logger
from ..models import Opportunity, OpportunityType, SearchQuery
from .base import SourceError

log = get_logger("sources.adzuna")

_BASE = "https://api.adzuna.com/v1/api/jobs"


class AdzunaSource:
    """Wrapper API Adzuna. Désactivé si les clés ne sont pas dans l'env."""

    name = "adzuna"

    def __init__(self, country: str = "fr", timeout: float = 15.0) -> None:
        self.country = country
        self.timeout = timeout
        self.app_id = os.environ.get("ADZUNA_APP_ID", "")
        self.app_key = os.environ.get("ADZUNA_APP_KEY", "")

    def is_enabled(self) -> bool:
        return bool(self.app_id and self.app_key)

    def search(self, query: SearchQuery) -> list[Opportunity]:
        if not self.is_enabled():
            log.info("Adzuna disabled: ADZUNA_APP_ID / ADZUNA_APP_KEY manquants")
            return []

        what = " ".join(query.keywords) or ""
        params = {
            "app_id": self.app_id,
            "app_key": self.app_key,
            "results_per_page": min(query.max_results_per_source, 50),
            "what": what,
            "max_days_old": query.posted_within_days,
            "content-type": "application/json",
        }
        if query.location:
            params["where"] = query.location

        url = f"{_BASE}/{self.country}/search/1"
        try:
            r = httpx.get(url, params=params, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as exc:
            raise SourceError(f"adzuna HTTP error: {exc}") from exc

        results: list[Opportunity] = []
        for item in data.get("results", []):
            try:
                results.append(_parse_item(item))
            except Exception as exc:  # noqa: BLE001
                log.debug("adzuna parse skip: %s", exc)
        log.info("Adzuna: %d results", len(results))
        return results


def _parse_item(item: dict) -> Opportunity:
    company = (item.get("company") or {}).get("display_name") or "Unknown"
    return Opportunity(
        url=item["redirect_url"],
        title=item.get("title", "(sans titre)").strip(),
        organization=company.strip(),
        description=(item.get("description") or "")[:4000],
        opportunity_type=OpportunityType.JOB,
        source="adzuna",
        raw_metadata={
            "salary_min": item.get("salary_min"),
            "salary_max": item.get("salary_max"),
            "category": (item.get("category") or {}).get("label"),
            "location": (item.get("location") or {}).get("display_name"),
            "created": item.get("created"),
        },
    )
