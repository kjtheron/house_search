"""Polite HTTP client used by every adapter.

Waits a random 5-10 s (config http.*) between requests *to the same site*, retries network
errors and 5xx,
and raises Blocked on 403/429 so the pipeline stops that source for the day.
It never rotates proxies or tries to get around a block.
"""

import logging
import random
import time
from urllib.parse import urlsplit

import httpx

from .config import HttpConfig

log = logging.getLogger(__name__)


class Blocked(Exception):
    """Site answered 403/429. Stop this source for today; never try to evade it."""


class PoliteClient:
    def __init__(self, cfg: HttpConfig, retries: int = 3, sleep=time.sleep, clock=time.monotonic):
        self.cfg, self.retries, self.sleep, self.clock = cfg, retries, sleep, clock
        self._last: dict[str, float] = {}  # host -> time of last request (each site has its own pace)
        self._client = httpx.Client(timeout=cfg.timeout_s, follow_redirects=True,
                                    headers={"User-Agent": cfg.user_agent, "Accept-Language": "en-ZA,en"})

    def _wait(self, host: str):
        if host in self._last:
            gap = random.uniform(self.cfg.min_delay_s, self.cfg.max_delay_s)
            left = self._last[host] + gap - self.clock()
            if left > 0:
                self.sleep(left)
        self._last[host] = self.clock()

    def get(self, url: str) -> httpx.Response:
        for attempt in range(1, self.retries + 1):
            self._wait(urlsplit(url).hostname)
            try:
                r = self._client.get(url)
            except httpx.TransportError as e:
                log.warning("GET %s failed (%s), attempt %d", url, e, attempt)
            else:
                if r.status_code in (403, 429):
                    raise Blocked(f"{r.status_code} from {url}")
                if r.status_code < 500:
                    return r
                log.warning("GET %s -> %d, attempt %d", url, r.status_code, attempt)
            if attempt < self.retries:
                self.sleep(30 * attempt)
        raise httpx.HTTPError(f"GET {url} failed after {self.retries} attempts")

    def close(self):
        self._client.close()
