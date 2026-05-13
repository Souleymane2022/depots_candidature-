"""Source France Travail (ex-Pôle Emploi) : API officielle.

Doc : https://francetravail.io/data/api
Clés : FRANCE_TRAVAIL_CLIENT_ID + FRANCE_TRAVAIL_CLIENT_SECRET (gratuit).

Flux : OAuth2 client_credentials → access_token → GET offres.
"""

from __future__ import annotations

import os
import time

import httpx

from ..logging_setup import get_logger
from ..models import Opportunity, OpportunityType, SearchQuery
from .base import SourceError

log = get_logger("sources.france_travail")

_TOKEN_URL = (
    "https://francetravail.io/connexion/oauth2/access_token?realm=%2Fpartenaire"
)
_SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"


class FranceTravailSource:
    """Wrapper API France Travail."""

    name = "france_travail"

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self.client_id = os.environ.get("FRANCE_TRAVAIL_CLIENT_ID", "")
        self.client_secret = os.environ.get("FRANCE_TRAVAIL_CLIENT_SECRET", "")
        self._token: str | None = None
        self._token_exp: float = 0.0

    def is_enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_exp - 30:
            return self._token
        try:
            r = httpx.post(
                _TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": "api_offresdemploiv2 o2dsoffre",
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            body = r.json()
        except httpx.HTTPError as exc:
            raise SourceError(f"france_travail token error: {exc}") from exc
        self._token = body["access_token"]
        self._token_exp = now + int(body.get("expires_in", 1500))
        return self._token

    def search(self, query: SearchQuery) -> list[Opportunity]:
        if not self.is_enabled():
            log.info(
                "France Travail disabled: FRANCE_TRAVAIL_CLIENT_ID/SECRET manquants"
            )
            return []

        token = self._get_token()
        params = {
            "motsCles": " ".join(query.keywords),
            "range": f"0-{min(query.max_results_per_source, 49)}",
            "publieeDepuis": query.posted_within_days,
        }
        if query.location:
            params["commune"] = query.location

        try:
            r = httpx.get(
                _SEARCH_URL,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout,
            )
            if r.status_code in (200, 206):
                data = r.json()
            else:
                raise SourceError(
                    f"france_travail HTTP {r.status_code}: {r.text[:200]}"
                )
        except httpx.HTTPError as exc:
            raise SourceError(f"france_travail HTTP error: {exc}") from exc

        results: list[Opportunity] = []
        for item in data.get("resultats", []):
            try:
                results.append(_parse(item))
            except Exception as exc:  # noqa: BLE001
                log.debug("france_travail parse skip: %s", exc)
        log.info("France Travail: %d results", len(results))
        return results


def _parse(item: dict) -> Opportunity:
    entreprise = (item.get("entreprise") or {}).get("nom") or "Anonyme"
    lieu = (item.get("lieuTravail") or {}).get("libelle") or ""
    return Opportunity(
        url=item.get("origineOffre", {}).get("urlOrigine") or item.get("id", ""),
        title=item.get("intitule", "(sans titre)"),
        organization=entreprise,
        description=(item.get("description") or "")[:4000],
        opportunity_type=OpportunityType.JOB,
        source="france_travail",
        language="fr",
        raw_metadata={
            "id": item.get("id"),
            "typeContrat": item.get("typeContrat"),
            "lieuTravail": lieu,
            "salaire": (item.get("salaire") or {}).get("libelle"),
            "dateCreation": item.get("dateCreation"),
        },
    )
