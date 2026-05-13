"""Source RSS / Atom : parse un flux et renvoie les entrées comme opportunités."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import feedparser

from ..logging_setup import get_logger
from ..models import Opportunity, OpportunityType, SearchQuery

log = get_logger("sources.rss")


class RssSource:
    """Source générique pour flux RSS / Atom."""

    def __init__(
        self,
        *,
        name: str,
        url: str,
        opportunity_type: OpportunityType = OpportunityType.EVENT,
    ) -> None:
        self.name = name
        self.url = url
        self.opportunity_type = opportunity_type

    def search(self, query: SearchQuery) -> list[Opportunity]:
        feed = feedparser.parse(self.url)
        if feed.bozo and not feed.entries:
            log.warning("RSS %s: parse error: %s", self.name, feed.bozo_exception)
            return []
        results: list[Opportunity] = []
        for entry in feed.entries[: query.max_results_per_source]:
            try:
                results.append(self._parse_entry(entry))
            except Exception as exc:  # noqa: BLE001
                log.debug("RSS %s parse skip: %s", self.name, exc)
        log.info("RSS %s: %d entries", self.name, len(results))
        return results

    def _parse_entry(self, entry: dict) -> Opportunity:
        title = entry.get("title") or "(sans titre)"
        link = entry.get("link") or ""
        summary = entry.get("summary") or entry.get("description") or ""
        author = entry.get("author") or _from_source(entry) or self.name
        return Opportunity(
            url=link,
            title=title,
            organization=author,
            description=summary[:4000],
            opportunity_type=self.opportunity_type,
            source=f"rss:{self.name}",
            raw_metadata={
                "published": _parse_date(entry),
                "feed": self.url,
            },
        )


def _from_source(entry: dict) -> str | None:
    src = entry.get("source")
    if isinstance(src, dict):
        return src.get("title")
    return None


def _parse_date(entry: dict) -> str | None:
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            return parsedate_to_datetime(raw).astimezone(UTC).isoformat()
        except (TypeError, ValueError):
            try:
                return datetime.fromisoformat(raw).astimezone(UTC).isoformat()
            except ValueError:
                continue
    return None
