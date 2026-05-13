"""Source browser-use : pour sites JS lourds (WTTJ, Hellowork, Devpost, F6S).

Lance patchright headful par défaut (compatible anti-bot), récupère les
URLs d'offres affichées sur la page, sans suivre de pagination complexe.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from urllib.parse import urljoin, urlparse

from ..logging_setup import get_logger
from ..models import Opportunity, OpportunityType, SearchQuery

log = get_logger("sources.browser")

_LINK_BLACKLIST = re.compile(
    r"(login|signin|signup|register|account|legal|privacy|terms|cookie)",
    re.IGNORECASE,
)


class BrowserSource:
    """Charge une page JS-lourde via patchright et extrait les liens d'opportunités."""

    def __init__(
        self,
        *,
        name: str,
        url: str,
        opportunity_type: OpportunityType = OpportunityType.JOB,
        headless: bool = True,
    ) -> None:
        self.name = name
        self.url = url
        self.opportunity_type = opportunity_type
        self.headless = headless

    def search(self, query: SearchQuery) -> list[Opportunity]:
        try:
            return asyncio.run(self._search_async(query))
        except RuntimeError as exc:
            if "event loop is already running" in str(exc).lower():
                loop = asyncio.get_event_loop()
                return loop.run_until_complete(self._search_async(query))
            raise

    async def _search_async(self, query: SearchQuery) -> list[Opportunity]:
        from patchright.async_api import async_playwright

        results: list[Opportunity] = []
        host = urlparse(self.url).hostname or ""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            ctx = await browser.new_context(locale="fr-FR")
            page = await ctx.new_page()
            try:
                await page.goto(self.url, wait_until="domcontentloaded", timeout=45000)
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("networkidle", timeout=8000)
                anchors = await page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => ({href: e.href, text: e.innerText.trim()}))",
                )
            finally:
                await ctx.close()
                await browser.close()

        seen: set[str] = set()
        for a in anchors:
            href = (a.get("href") or "").strip()
            text = (a.get("text") or "").strip()
            if not href or not text or len(text) < 4:
                continue
            if _LINK_BLACKLIST.search(href):
                continue
            full = urljoin(self.url, href)
            parsed = urlparse(full)
            if host and parsed.hostname and host not in parsed.hostname:
                continue
            if full in seen:
                continue
            seen.add(full)
            results.append(
                Opportunity(
                    url=full,
                    title=text[:200],
                    organization=host.replace("www.", "").split(".")[0].capitalize(),
                    description=text[:1000],
                    opportunity_type=self.opportunity_type,
                    source=f"browser:{self.name}",
                    raw_metadata={"parent_url": self.url},
                )
            )
            if len(results) >= query.max_results_per_source:
                break

        log.info("Browser %s: %d candidates", self.name, len(results))
        return results
