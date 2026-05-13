"""Sources de découverte d'opportunités (jobs, contests, events, cfps)."""

from .base import OpportunitySource, SourceError
from .registry import build_from_api_config, build_from_watch_source

__all__ = [
    "OpportunitySource",
    "SourceError",
    "build_from_watch_source",
    "build_from_api_config",
]
