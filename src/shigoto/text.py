"""Small text helpers shared by adapters and normalization."""

from __future__ import annotations

import html
import re
from datetime import date, datetime

from bs4 import BeautifulSoup

_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n\s*\n+")


def clean(value: object) -> str:
    """Collapse whitespace; treat None/NaN-ish values as empty."""
    if value is None:
        return ""
    s = str(value)
    if s.lower() in {"nan", "none", "nat"}:
        return ""
    return _WS.sub(" ", s).strip()


def html_to_text(markup: str) -> str:
    """Convert posting HTML to readable plain text, keeping paragraph/list breaks."""
    if not markup:
        return ""
    if "&lt;" in markup and "<" not in markup:
        markup = html.unescape(markup)  # Greenhouse double-escapes its content
    soup = BeautifulSoup(markup, "html.parser")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text("\n")
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def clean_description(value: str) -> str:
    lines = [_WS.sub(" ", line).strip() for line in value.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def parse_date(value: object) -> date | None:
    """Parse ISO dates/datetimes, epoch millis, or date objects; None if unparseable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000).date() if value > 0 else None
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None
