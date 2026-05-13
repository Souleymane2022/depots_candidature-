"""Tests : JobFinder (dédup, persistence, filtres) + canonical_url."""

from __future__ import annotations

import json
from pathlib import Path

from job_agent import db, llm
from job_agent.job_finder import (
    JobFinder,
    canonical_url,
    import_watch_list_from_json,
)
from job_agent.models import (
    FundingInfo,
    FundingType,
    Opportunity,
    OpportunityType,
    OpportunityTypeDetection,
    SearchQuery,
)


def test_canonical_url_strips_trailing_slash_and_lowercases_host():
    assert canonical_url("https://Example.COM/path/") == "https://example.com/path"
    assert canonical_url("https://example.com/path") == "https://example.com/path"
    assert canonical_url("https://example.com") == "https://example.com/"


class _StubSource:
    name = "stub"

    def __init__(self, items: list[Opportunity]) -> None:
        self._items = items

    def search(self, query: SearchQuery) -> list[Opportunity]:
        return list(self._items)


def _make_opp(url: str, **kwargs) -> Opportunity:
    return Opportunity(
        url=url,
        title=kwargs.get("title", "Backend Engineer"),
        organization=kwargs.get("organization", "Acme"),
        description=kwargs.get("description", "We use Python."),
        opportunity_type=kwargs.get("opportunity_type", OpportunityType.JOB),
        source=kwargs.get("source", "stub"),
    )


def test_persist_dedups_and_inserts(temp_db):
    opps = [
        _make_opp("https://acme.com/jobs/1"),
        _make_opp("https://acme.com/jobs/1/"),  # doublon canonique
        _make_opp("https://acme.com/jobs/2"),
    ]
    finder = JobFinder(temp_db)
    inserted = finder._persist([(o, False) for o in opps], SearchQuery())
    assert inserted == 2
    rows = list(temp_db.execute("SELECT job_url FROM applications ORDER BY id"))
    urls = {r["job_url"] for r in rows}
    assert urls == {"https://acme.com/jobs/1", "https://acme.com/jobs/2"}


def test_persist_skips_already_known_urls(temp_db):
    db.insert_application(
        temp_db, company="Acme", job_title="X", job_url="https://acme.com/jobs/1"
    )
    finder = JobFinder(temp_db)
    inserted = finder._persist(
        [(_make_opp("https://acme.com/jobs/1"), False)], SearchQuery()
    )
    assert inserted == 0


def test_persist_filters_by_opportunity_type(temp_db, monkeypatch):
    # On force la détection LLM à dire "contest" pour une opportunité venant d'une source RSS
    monkeypatch.setattr(
        llm,
        "detect_opportunity_type",
        lambda *a, **kw: OpportunityTypeDetection(
            opportunity_type=OpportunityType.CONTEST, confidence=0.9
        ),
    )
    opps = [_make_opp("https://x.com/1", source="rss:devpost")]
    finder = JobFinder(temp_db)
    inserted = finder._persist(
        [(o, False) for o in opps],
        SearchQuery(opportunity_types=[OpportunityType.JOB]),
    )
    assert inserted == 0  # filtré : on demande JOB, la détection dit CONTEST


def test_persist_applies_free_funded_filter(temp_db, monkeypatch):
    monkeypatch.setattr(
        llm,
        "extract_funding",
        lambda title, desc: FundingInfo(
            is_free_to_enter=False,
            is_funded=False,
            funding_type=FundingType.NONE,
        ),
    )
    finder = JobFinder(temp_db)
    inserted = finder._persist(
        [(_make_opp("https://x.com/1"), True)],
        SearchQuery(),
    )
    assert inserted == 0  # filtré : pas gratuit ET pas financé


def test_persist_passes_funding_filter_when_free_and_funded(temp_db, monkeypatch):
    monkeypatch.setattr(
        llm,
        "extract_funding",
        lambda title, desc: FundingInfo(
            is_free_to_enter=True,
            is_funded=True,
            funding_amount_eur=5000,
            funding_type=FundingType.PRIZE,
        ),
    )
    finder = JobFinder(temp_db)
    inserted = finder._persist(
        [(_make_opp("https://x.com/1"), True)],
        SearchQuery(),
    )
    assert inserted == 1


def test_persist_min_funding(temp_db, monkeypatch):
    monkeypatch.setattr(
        llm,
        "extract_funding",
        lambda title, desc: FundingInfo(
            is_free_to_enter=True,
            is_funded=True,
            funding_amount_eur=1000,
            funding_type=FundingType.PRIZE,
        ),
    )
    finder = JobFinder(temp_db)
    inserted = finder._persist(
        [(_make_opp("https://x.com/1"), False)],
        SearchQuery(free_only=True, min_funding_eur=5000),
    )
    assert inserted == 0


def test_import_watch_list_from_json(temp_db, tmp_path: Path):
    path = tmp_path / "watch.json"
    path.write_text(
        json.dumps({
            "sources": [
                {
                    "name": "Devpost",
                    "url": "https://devpost.com/hackathons",
                    "method": "browser_use",
                    "opportunity_type": "contest",
                    "refresh_hours": 24,
                    "free_funded_only": True,
                    "enabled": True,
                    "notes": "Free hackathons only",
                },
                {
                    "name": "Bpifrance",
                    "url": "https://www.bpifrance.fr/appels-a-projets",
                    "method": "html_scrape",
                    "opportunity_type": "cfp",
                    "refresh_hours": 48,
                    "free_funded_only": True,
                    "enabled": True,
                },
            ]
        })
    )
    n = import_watch_list_from_json(temp_db, path)
    assert n == 2
    rows = db.list_watch_sources(temp_db)
    names = {r["name"] for r in rows}
    assert names == {"Devpost", "Bpifrance"}


def test_search_with_stub_source(temp_db, monkeypatch):
    """search() doit appeler les sources DB activées et persister les résultats."""
    # On injecte une source stub dans la registry
    from job_agent import job_finder as jf

    db.upsert_watch_source(
        temp_db,
        name="StubSource",
        url="https://x.com",
        method="html_scrape",
        opportunity_type="job",
    )
    stub_opps = [_make_opp("https://x.com/jobs/1"), _make_opp("https://x.com/jobs/2")]

    def fake_build(row):
        return _StubSource(stub_opps)

    monkeypatch.setattr(jf, "build_from_watch_source", fake_build)
    monkeypatch.setattr(jf, "build_from_api_config", lambda: [])

    finder = jf.JobFinder(temp_db)
    stats = finder.search(SearchQuery(keywords=["python"]), include_apis=False)
    assert stats["StubSource"] == 2
    assert stats["_inserted_total"] == 2

    # Vérifie que last_run_at a été mis à jour
    row = db.get_watch_source(temp_db, "StubSource")
    assert row["last_run_at"] is not None


def test_search_marks_failing_source_as_error(temp_db, monkeypatch):
    from job_agent import job_finder as jf

    db.upsert_watch_source(
        temp_db, name="BrokenSource", url="https://x", method="html_scrape"
    )

    class _Broken:
        name = "broken"

        def search(self, query):
            raise RuntimeError("boom")

    monkeypatch.setattr(jf, "build_from_watch_source", lambda row: _Broken())
    monkeypatch.setattr(jf, "build_from_api_config", lambda: [])

    finder = jf.JobFinder(temp_db)
    stats = finder.search(SearchQuery(), include_apis=False)
    assert stats["BrokenSource"] == -1
