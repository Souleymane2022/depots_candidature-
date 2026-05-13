"""Construction d'une OpportunitySource à partir d'une WatchSource (row DB)."""

from __future__ import annotations

import sqlite3

from ..models import OpportunityType, WatchSourceMethod
from .adzuna import AdzunaSource
from .base import OpportunitySource
from .browser_source import BrowserSource
from .france_travail import FranceTravailSource
from .html_source import HtmlSource
from .rss_source import RssSource


def build_from_watch_source(row: sqlite3.Row) -> OpportunitySource:
    """Factory : convertit une ligne `watch_sources` en source instanciée."""
    method = WatchSourceMethod(row["method"])
    opp_type = OpportunityType(row["opportunity_type"])
    name = row["name"]
    url = row["url"]
    if method == WatchSourceMethod.RSS:
        return RssSource(name=name, url=url, opportunity_type=opp_type)
    if method == WatchSourceMethod.HTML_SCRAPE:
        return HtmlSource(name=name, url=url, opportunity_type=opp_type)
    if method == WatchSourceMethod.BROWSER_USE:
        return BrowserSource(name=name, url=url, opportunity_type=opp_type)
    if method == WatchSourceMethod.API_ADZUNA:
        return AdzunaSource()
    if method == WatchSourceMethod.API_FRANCE_TRAVAIL:
        return FranceTravailSource()
    raise ValueError(f"Unknown source method: {method}")


def build_from_api_config() -> list[OpportunitySource]:
    """Renvoie les sources API si leurs clés sont présentes dans l'env."""
    sources: list[OpportunitySource] = []
    adzuna = AdzunaSource()
    if adzuna.is_enabled():
        sources.append(adzuna)
    ft = FranceTravailSource()
    if ft.is_enabled():
        sources.append(ft)
    return sources
