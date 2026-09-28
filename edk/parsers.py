"""Parsers for HTML sources that the JSON API does not structure for us."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from bs4 import BeautifulSoup

REASONS = {"e": "parliamentary_work", "h": "personal"}
_TITLE_RE = re.compile(r"Nimenhuutoraportti\s+\w+\s+(\d{1,2})\.(\d{1,2})\.(\d{4})\s+klo\s+(\d{1,2})\.(\d{2})")
_COUNT_RE = re.compile(r"seuraavat\s+(\d+)\s+edustaja")
_PAREN_RE = re.compile(r"^\((.*)\)$")
_TIME_RE = re.compile(r"^(\d{1,2})[.:](\d{2})$")


def clean(s: str | None) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def norm_name(s: str) -> str:
    """Normalised key for joining name-only sources to MP ids."""
    s = unicodedata.normalize("NFC", clean(s)).replace("*", "").lower()
    return re.sub(r"\s+", " ", s).strip()


def parse_rollcall(html: str) -> dict:
    """Parse a 'Nimenhuutoraportti' (plenary roll-call report, Liite 1A).

    Returns {"session_date", "session_start", "sections": [...], "people": [...]}
    where each person row is one absent MP or one late arrival.
    Absence codes: (e) parliamentary work, (h) personal reason, none = other (no reason given).
    Late arrivals carry the registration time, e.g. (10.18).
    """
    soup = BeautifulSoup(html, "html.parser")
    text = clean(soup.get_text(" "))
    m = _TITLE_RE.search(text)
    session_date = session_start = None
    if m:
        d, mo, y, hh, mm = map(int, m.groups())
        session_date = f"{y:04d}-{mo:02d}-{d:02d}"
        session_start = f"{hh:02d}:{mm:02d}"

    sections, people = [], []
    for osa in soup.select("div.OsallistujaOsa"):
        p = osa.find_previous_sibling("p")
        intro = clean(p.get_text(" ")) if p else ""
        low = intro.lower()
        if "poissa" in low:
            kind = "absent"
        elif "jälkeen" in low or "ilmoittautu" in low:
            kind = "late_arrival"
        else:
            kind = "other"
        cm = _COUNT_RE.search(low)
        expected = int(cm.group(1)) if cm else None
        rows = []
        for t in osa.select("span.Toimija"):
            first = clean(t.select_one(".EtuNimi").get_text(" ")) if t.select_one(".EtuNimi") else ""
            last = clean(t.select_one(".SukuNimi").get_text(" ")) if t.select_one(".SukuNimi") else ""
            party, code = None, None
            for x in t.select(".LisatietoTeksti"):
                v = clean(x.get_text(" "))
                pm = _PAREN_RE.match(v)
                if pm:
                    code = pm.group(1).strip()
                elif v:
                    party = v
            row = {"kind": kind, "first_name": first, "last_name": last,
                   "name": clean(f"{first} {last}"), "party_abbr": party}
            if kind == "absent":
                row["reason_code"] = code or ""
                row["reason"] = REASONS.get((code or "").lower(), "other" if not code else code)
            elif kind == "late_arrival":
                row["arrival_time"] = None
                row["minutes_late"] = None
                tm = _TIME_RE.match(code or "")
                if tm:
                    row["arrival_time"] = f"{int(tm.group(1)):02d}:{tm.group(2)}"
                    if session_start:
                        a = datetime.strptime(row["arrival_time"], "%H:%M")
                        s = datetime.strptime(session_start, "%H:%M")
                        row["minutes_late"] = int((a - s).total_seconds() // 60)
            rows.append(row)
        sections.append({"kind": kind, "intro": intro, "expected": expected, "parsed": len(rows)})
        people.extend(rows)
    return {"session_date": session_date, "session_start": session_start,
            "sections": sections, "people": people}


# ------------------------------------------------------------- Datawrapper
_DW_VERSION_RE = r"https://datawrapper\.dwcdn\.net/{id}/(\d+)/"
_DW_LINK_RE = re.compile(r"datawrapper\.dwcdn\.net\\?/([A-Za-z0-9]{5})(?![A-Za-z0-9])")
_OG_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]*)"', re.I)
COMMITTEE_TITLE_RE = re.compile(r"Poissaolot\s+(\d{4})\s+\(([^)]+)\)")


def dw_version_url(chart_id: str, base_html: str, final_url: str = "") -> str | None:
    """The un-versioned chart URL answers with a meta-refresh to /<id>/<version>/."""
    pat = re.compile(_DW_VERSION_RE.format(id=re.escape(chart_id)))
    if pat.match(final_url or ""):
        return pat.match(final_url).group(0)
    m = pat.search(base_html or "")
    return m.group(0) if m else None


def dw_title(html: str) -> str:
    m = _OG_TITLE_RE.search(html or "")
    return clean(m.group(1)) if m else ""


def dw_links(html: str) -> list[str]:
    return sorted(set(_DW_LINK_RE.findall(html or "")))


def parse_fi_date(s: str) -> str | None:
    """'16.05.2023@@16.05.2023' or '16.05.2023' -> '2023-05-16'."""
    s = clean(str(s)).split("@@")[0]
    try:
        return datetime.strptime(s, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None
