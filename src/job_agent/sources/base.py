"""Interface commune à toutes les sources d'opportunités."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Opportunity, SearchQuery


class SourceError(RuntimeError):
    """Erreur récupérable d'une source (réseau, parsing, quota...)."""


@runtime_checkable
class OpportunitySource(Protocol):
    """Toute source de découverte (API, RSS, scraping HTML/JS)."""

    name: str

    def search(self, query: SearchQuery) -> list[Opportunity]:
        """Renvoie les opportunités correspondant à la recherche."""
        ...
