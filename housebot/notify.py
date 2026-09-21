"""Format listing alerts and send them.

Telegram sends messages with the Bot API (send-only). It uses the same bot as PicoClaw:
PicoClaw receives your replies ("fav 142"), housebot only pushes the daily alerts.
Console prints the messages instead; `housebot run --dry-run` uses it.
"""

import html
import logging
import os
import time

import httpx

log = logging.getLogger(__name__)


def rand(p: int | None) -> str:
    return "POA" if p is None else f"R {p:,}".replace(",", " ")


def _num(x) -> str:
    return f"{x:g}" if isinstance(x, float) else str(x)


def format_listing(l: dict) -> str:
    e = lambda s: html.escape(str(s), quote=False)  # Telegram HTML needs only <, >, & escaped
    where = ", ".join(e(x) for x in (l.get("town"), l.get("suburb")) if x)
    if l.get("reason") == "price_change" and l.get("last_price") and l.get("price"):
        old, new = l["last_price"], l["price"]
        icon, word = ("📉", "PRICE DROP") if new < old else ("📈", "PRICE UP")
        head = f"{icon} <b>{word} #{l['id']}</b>: {rand(old)} → {rand(new)} ({(new - old) / old:+.1%})\n{where}"
    else:
        head = f"🏠 <b>NEW  #{l['id']}</b> · {where}"
    facts = [f"<b>{rand(l.get('price'))}</b>"]
    if l.get("property_type"):
        facts.append(l["property_type"].replace("_", " ").title())
    facts += [f"{_num(l[k])} {w}" for k, w in (("beds", "bed"), ("baths", "bath"), ("garages", "garage"))
              if l.get(k) is not None]
    sizes = [f"{w} {l[k]} m²" for k, w in (("floor_m2", "Floor"), ("erf_m2", "Erf")) if l.get(k)]
    lines = [head, " · ".join(facts)]
    if sizes:
        lines.append(" · ".join(sizes))
    if l.get("agency"):
        lines.append(f"Agency: {e(l['agency'])}")
    lines += [e(l["url"]), f'Reply to me: "fav {l["id"]}" or "hide {l["id"]}"']
    return "\n".join(lines)


class Telegram:
    """Send-only. Never calls getUpdates: PicoClaw owns the receiving side of the bot."""

    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        if not (self.token and self.chat_id):
            raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (see .env.example)")
        self._last = 0.0

    def _call(self, method: str, **data) -> int:
        time.sleep(max(0.0, self._last + 1.1 - time.monotonic()))  # <= 1 msg/s
        self._last = time.monotonic()
        r = httpx.post(f"https://api.telegram.org/bot{self.token}/{method}", data=data, timeout=30)
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body.get('description')}")
        return body["result"]["message_id"]

    def send(self, text: str, photo: str | None = None) -> int:
        if photo:
            try:
                return self._call("sendPhoto", chat_id=self.chat_id, photo=photo, caption=text, parse_mode="HTML")
            except RuntimeError as e:
                log.warning("%s, sending as text", e)
        return self._call("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML",
                          disable_web_page_preview="true")


class Console:
    """Dry-run notifier: prints instead of sending."""

    def send(self, text: str, photo: str | None = None) -> None:
        print(text + (f"\n[photo] {photo}" if photo else "") + "\n")
