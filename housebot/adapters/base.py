"""Shared pieces for source adapters: the Page result, the adapter protocol, URL helpers.

An adapter builds search URLs from the config, fetches pages with PoliteClient, and yields
one parsed Page per results page. Each adapter is isolated, so a site change breaks only it.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol

from ..config import SearchConfig, SourceConfig
from ..http import PoliteClient
from ..models import Listing


@dataclass
class Page:
    """Result of parsing one search results page."""
    listings: list[Listing] = field(default_factory=list)
    errors: int = 0
    html: str = ""  # kept so a broken run can save it to data/debug/


class SourceAdapter(Protocol):
    name: str
    complete: bool  # True after pages() if every result page was read (safe to mark missing listings gone)

    def __init__(self, http: PoliteClient, src: SourceConfig): ...

    def pages(self, cfg: SearchConfig, known: set[str] = frozenset()) -> Iterator[Page]: ...


class BaseAdapter:
    """Paging loop shared by the site adapters. Subclasses give the URL, parser and next-page check.

    Searches each town in `sources.<name>.locations`, or the whole province when that map is empty.
    newest_first sites stop at the first page with no unknown IDs: on a daily run that is
    usually page 1-3, so a province-wide search stays cheap.
    """
    name = ""
    newest_first = False

    def __init__(self, http: PoliteClient, src: SourceConfig):
        self.http, self.src = http, src
        self.complete = True

    def search_url(self, cfg: SearchConfig, town: str | None, loc_id: int, ptype: str, page: int) -> str: ...
    def parse(self, html: str, town: str | None, province: str) -> Page: ...
    def has_next(self, html: str, page: int) -> bool: ...
    def listing_status(self, url: str) -> str: ...   # fetch one listing page: active|under_offer|sold|gone

    def pages(self, cfg: SearchConfig, known: set[str] = frozenset()) -> Iterator[Page]:
        targets = list(self.src.locations.items()) or [(None, self.src.province_id)]
        self.complete = True
        for town, loc_id in targets:
            for ptype in cfg.property_types:
                seen: set[str] = set()
                for n in range(1, self.src.max_pages + 1):
                    r = self.http.get(self.search_url(cfg, town, loc_id, ptype, n))
                    if r.status_code == 404:
                        break
                    page = self.parse(r.text, town, cfg.province)
                    yield page
                    # Promoted tiles are out of date order, so they don't count toward "caught up".
                    ids = {l.source_listing_id for l in page.listings if not l.raw.get("promoted")}
                    if not ids - seen or not self.has_next(r.text, n):
                        break  # natural end of results
                    if self.newest_first and known and not ids - known:
                        self.complete = False  # caught up with yesterday
                        break
                    seen |= ids
                else:
                    self.complete = False  # hit max_pages


def slug(text: str) -> str:
    return "-".join(text.lower().split())


# Site URL words per config property type.
TYPE_SLUGS = {"house": "houses", "townhouse": "townhouses", "apartment": "apartments",
              "vacant_land": "vacant-land", "farm": "farms"}
