"""Source HTML : scrape une page de listing avec BeautifulSoup + LLM extraction.

Convient aux pages careers d'entreprises et aux pages de listings statiques
(Bpifrance appels à projets, Horizon Europe, etc.).
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..logging_setup import get_logger
from ..models import Opportunity, OpportunityType, SearchQuery
from .base import SourceError

log = get_logger("sources.html")

_OFFER_HREF_PATTERNS = (
    re.compile(r"/(jobs?|career(s)?|emploi|offre|apply|hackathon|contest|prize|appel|aap|cfp)/", re.IGNORECASE),
    re.compile(r"/(opportunit(y|ies))/", re.IGNORECASE),
)


class HtmlSource:
    """Scrape une page HTML et extrait les liens d'opportunités."""

    def __init__(
        self,
        *,
        name: str,
        url: str,
        opportunity_type: OpportunityType = OpportunityType.JOB,
        timeout: float = 15.0,
        user_agent: str | None = None,
    ) -> None:
        self.name = name
        self.url = url
        self.opportunity_type = opportunity_type
        self.timeout = timeout
        self.user_agent = (
            user_agent
            or "Mozilla/5.0 (compatible; job-agent/0.1; +https://github.com/)"
        )

    def search(self, query: SearchQuery) -> list[Opportunity]:
        try:
            r = httpx.get(
                self.url,
                headers={"User-Agent": self.user_agent, "Accept-Language": "fr,en;q=0.9"},
                timeout=self.timeout,
                follow_redirects=True,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceError(f"html_source {self.name} HTTP error: {exc}") from exc

        soup = BeautifulSoup(r.text, "html.parser")
        org = _extract_organization(soup, self.url)
        seen: set[str] = set()
        results: list[Opportunity] = []

        for link in soup.find_all("a", href=True):
            href = link["href"].strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            full = urljoin(self.url, href)
            if full in seen:
                continue
            if not any(p.search(full) for p in _OFFER_HREF_PATTERNS):
                continue
            text = " ".join(link.get_text(" ", strip=True).split())
            if not text or len(text) < 4:
                continue
            seen.add(full)
            results.append(
                Opportunity(
                    url=full,
                    title=text[:200],
                    organization=org,
                    description=text[:1000],
                    opportunity_type=self.opportunity_type,
                    source=f"html:{self.name}",
                    raw_metadata={"parent_url": self.url},
                )
            )
            if len(results) >= query.max_results_per_source:
                break

        log.info("HTML %s: %d candidates", self.name, len(results))
        return results


def _extract_organization(soup: BeautifulSoup, url: str) -> str:
    """Heuristique : og:site_name → <title> → domaine."""
    meta = soup.find("meta", attrs={"property": "og:site_name"})
    if meta and meta.get("content"):
        return meta["content"].strip()
    if soup.title and soup.title.string:
        text = soup.title.string.strip()
        for sep in (" - ", " | ", " — "):
            if sep in text:
                return text.split(sep, 1)[-1].strip()
        return text[:80]
    host = urlparse(url).hostname or url
    return host.replace("www.", "").split(".")[0].capitalize()
