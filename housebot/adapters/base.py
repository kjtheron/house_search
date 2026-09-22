"""Shared pieces for source adapters: the Page result, the paging loop, card-parsing helpers.

An adapter builds search URLs from the config, fetches pages with PoliteClient, and yields
one parsed Page per results page. Each adapter is isolated, so a site change breaks only it.
The site modules only hold what differs per site: URLs, CSS selectors, field names.
"""

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

from selectolax.parser import HTMLParser, Node

from ..config import SearchConfig, SourceConfig
from ..http import PoliteClient
from ..models import Listing, badge_status, to_float, to_int

log = logging.getLogger(__name__)


@dataclass
class Page:
    """Result of parsing one search results page."""
    listings: list[Listing] = field(default_factory=list)
    errors: int = 0
    html: str = ""  # kept so a broken run can save it to data/debug/


class BaseAdapter:
    """Paging loop shared by the site adapters. Subclasses give the URL, parser and next-page marker.

    Searches each town in `sources.<name>.locations`, or the whole province when that map is empty.
    newest_first sites stop at the first page with no unknown IDs: on a daily run that is
    usually page 1-3, so a province-wide search stays cheap.
    """
    name = ""
    newest_first = False
    next_page = ""  # text in the HTML that proves page N+1 exists, formatted with N+1

    def __init__(self, http: PoliteClient, src: SourceConfig):
        self.http, self.src = http, src
        self.complete = True  # True after pages() if every result page was read (safe to mark gone)

    def search_url(self, cfg: SearchConfig, town: str | None, loc_id: int, ptype: str, page: int) -> str: ...
    def parse(self, html: str) -> Page: ...
    def details(self, url: str) -> dict: ...  # fetch one listing page: Listing fields + "status" (gone if removed)
    def town_ids(self, province: str) -> dict[str, int]: ...

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
                    if town and slug(town) not in str(r.url):  # site redirected: wrong location ID
                        raise ValueError(f"location ID {loc_id} is not {town} on {self.name} "
                                         f"(site sent {r.url.path}); fix sources.{self.name}.locations")
                    page = self.parse(r.text)
                    yield page
                    # Promoted cards are out of date order, so they don't count toward "caught up".
                    ids = {l.source_listing_id for l in page.listings if not l.raw.get("promoted")}
                    if not ids - seen or self.next_page.format(n + 1) not in r.text:
                        break  # natural end of results
                    if self.newest_first and known and not ids - known:
                        self.complete = False  # caught up with yesterday
                        break
                    seen |= ids
                else:
                    self.complete = False  # hit max_pages


# --- card parsing helpers -------------------------------------------------------

def parse_cards(html: str, selector: str, card: Callable[[Node], Listing | None], source: str) -> Page:
    """Run `card` on every node matching `selector`; None = not a listing (an ad); a bad card is counted."""
    page = Page(html=html)
    for node in HTMLParser(html).css(selector):
        try:
            l = card(node)
            if l is not None:
                page.listings.append(l)
        except Exception as e:
            log.warning("%s: skipped card: %s", source, e)
            page.errors += 1
    return page


def finish_card(l: Listing, node: Node, features: dict[str, str], feature_sel: str, promoted: bool) -> Listing:
    """Fill what both sites expose the same way: sale badge, promoted flag, auction, feature icons."""
    l.status = badge_status(node)
    if promoted:
        l.raw["promoted"] = True  # promoted/featured cards, shown out of date order
    if re.search(r"\bauction\b", node.text().lower()):
        l.listing_kind = "auction"
    for f in node.css(feature_sel):
        name = features.get(f.attributes["title"])
        if name:
            val = f.text(strip=True)
            setattr(l, name, to_float(val) if name in ("beds", "baths") else to_int(val))
    return l


def text(node: Node, sel: str) -> str | None:
    n = node.css_first(sel)
    return (n.text(strip=True) or None) if n else None


def parse_date(text: str | None) -> str | None:
    """ "22 September 2026" / "22 Sep 2026" -> "2026-09-22"."""
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime((text or "").strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def slug(s: str) -> str:
    """URL form of a place name: "Gordon's Bay" -> "gordons-bay"."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower().replace("'", "")).strip("-")


# Site URL words per config property type.
TYPE_SLUGS = {"house": "houses", "townhouse": "townhouses", "apartment": "apartments",
              "vacant_land": "vacant-land", "farm": "farms"}
