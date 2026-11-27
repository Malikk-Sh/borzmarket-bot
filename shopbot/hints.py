"""Явные поля из подписи к одной карточке или из быстрого ввода."""
import re
from dataclasses import replace

from card import parse_price, short_text
from store_prices import shared_quote

URL = re.compile(r"https?://[^\s<>]+", re.I)
LABEL = re.compile(r"^(?:название|товар)\s*:\s*(.+)$", re.I)
INTRO = re.compile(r"^(?:смотри(?:те)?|посмотри(?:те)?|глянь(?:те)?|наш[её]л|держи|смотрите, что)\b", re.I)


def fields(product, value, *, quick=False):
    lines = [line.strip() for line in URL.sub("", value).splitlines() if line.strip()]
    if not lines or (len(lines) == 1 and lines[0].startswith("/")):
        return "", None
    quoted = shared_quote(product, "\n".join(lines))
    title, amount = "", None
    names = []
    for line in lines:
        if match := LABEL.fullmatch(line):
            title = match[1]
        elif INTRO.match(line) or line.endswith("?"):
            continue
        elif match := re.fullmatch(r"(.+?)\s+[—–-]\s+(.+)", line):
            if (candidate := parse_price(match[2])) is not None:
                names.append(match[1])
                amount = candidate
            else:
                names.append(line)
        elif ":" in line or re.search(r"(?:₽|\bруб\b|\bкошел[ье]|\bбанками\b)", line, re.I):
            if (candidate := parse_price(line)) is not None:
                amount = candidate
        elif (candidate := parse_price(line)) is not None:
            if quick or len(lines) > 1:
                amount = candidate
        elif not line.startswith("/") and any(c.isalpha() for c in line):
            names.append(line)
    if not title and len(names) == 1:
        title = names[0]
    if quoted is product and amount is not None:
        quoted = replace(product, price=amount, price_note="", old_price=None, price_source="message",
                         conditional_price=None, conditional_note="", price_unavailable=False)
    return short_text(title, 240), None if quoted is product else quoted
