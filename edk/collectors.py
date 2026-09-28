"""Step 1: download raw data. Everything is cached under <out>/raw so re-runs are cheap
and a daily refresh only fetches what is new."""
from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

import pandas as pd

from . import config, parsers
from .client import Client, read_jsonl, unwrap, write_jsonl

log = logging.getLogger(__name__)

REFERENCE_ENDPOINTS = [
    "asiakirjatyypit", "asiatyypit", "eduskuntaryhmat", "puheenvuorotyypit",
    "sukupuolet", "vaalikaudet", "vaalipiirit", "valiokunnat", "valtiopaivat", "kansanedustajat",
]


def _date_range(prop: str) -> dict:
    return {"property": prop, "fromDate": config.TERM_START, "toDate": config.TERM_END}


def _save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------- reference
def collect_reference(c: Client, raw: Path) -> None:
    for name in REFERENCE_ENDPOINTS:
        data = c.get_json(f"reference-data/{name}")
        _save_json(raw / "reference" / f"{name}.json", data)
    log.info("reference data: %d endpoints", len(REFERENCE_ENDPOINTS))


# --------------------------------------------------------------------- votes
def collect_votes(c: Client, raw: Path) -> None:
    """All plenary votes of the term with every MP's ballot.

    Verified live (Sept 2026):
    - GET /taysistunnot/istunnon-aanestykset/{session} returns at most 10 votes, so it
      silently drops most votes of long (e.g. budget) sessions - don't use it.
    - A date-range criterion on `istuntopvm` misses votes. Filter on `istuntovpvuosi`
      (parliamentary year) instead, and post-filter on the exact value and term start.
    Full records are paged 100 at a time (~30 rate-limited POSTs for a whole term).
    Past years are cached; the current year is always refreshed.
    """
    first_year, this_year = int(config.TERM_START[:4]), int(config.TERM_END[:4])
    (raw / "votes").mkdir(parents=True, exist_ok=True)
    total = 0
    for year in range(first_year, this_year + 1):
        path = raw / "votes" / f"{year}-all.json"
        if path.exists() and year < this_year:
            total += len(_load_json(path))
            continue
        rows, start = [], 0
        while True:
            batch = c.search_page({"category": "aanestys", "maxResults": 100, "startFromIndex": start,
                                   "expression": {"property": "istuntovpvuosi", "match": str(year)},
                                   "sort": [{"property": "id", "ascending": True}]})
            recs = [unwrap(x, "aanestys") for x in batch]
            rows += [v for v in recs if v.get("istuntovpvuosi") == str(year)
                     and (v.get("istuntopvm") or "")[:10] >= config.TERM_START]
            if len(batch) < 100 or start + 200 > 10000:
                break
            start += 100
        seen, uniq = set(), []
        for v in rows:
            if v["id"] not in seen:
                seen.add(v["id"])
                uniq.append(v)
        _save_json(path, uniq)
        total += len(uniq)
        log.info("votes %d: %d", year, len(uniq))
    log.info("votes: %d in total", total)


# ---------------------------------------------------------------- roll calls
def collect_rollcalls(c: Client, raw: Path) -> None:
    """Roll-call reports (Nimenhuutoraportti, doc type NHR): absences with reason codes
    including parliamentary-work absences, plus late arrivals with registration time."""
    expr = {"and": [{"property": "asiakirjatyyppikoodi", "match": "NHR"}, _date_range("laadintapvm")]}
    docs = c.search_all("asiakirja", expr, sort=[{"property": "laadintapvm", "ascending": True}],
                        fields={"operation": "exclude", "list": ["fullText", "fullTextSnippet"]})
    docs = [d for d in docs if d.get("asiakirjatyyppikoodi") == "NHR" and d.get("kielikoodi") == config.LANG]
    _save_json(raw / "rollcalls" / "_index.json", docs)
    for i, d in enumerate(docs, 1):
        path = raw / "rollcalls" / f"{d['edktunnus']}.html"
        if path.exists():
            continue
        html = c.get_text(f"asiakirjat/edktunnus/{d['edktunnus']}/html")
        path.write_text(html or "", encoding="utf-8")
        if i % 50 == 0:
            log.info("roll calls: %d/%d", i, len(docs))
    log.info("roll calls: %d reports", len(docs))


# ------------------------------------------------------------------ speeches
def collect_speeches(c: Client, raw: Path) -> None:
    rows = c.dataset("puheenvuoro", _date_range("aloitushetki"),
                     sort=[{"property": "aloitushetki", "ascending": True}])
    n = write_jsonl(raw / "speeches.jsonl", rows)
    log.info("speeches: %d", n)


# ------------------------------------------------------------------- matters
def collect_matters(c: Client, raw: Path) -> None:
    """Parliamentary matters of the term: government bills, MPs' motions, written
    questions, citizens' initiatives, interpellations, budget motions ..."""
    expr = {"property": "vaalikausitunnus", "match": config.TERM_ID}
    rows = c.search_all("valtiopaivaasia", expr, sort=[{"property": "laadintapvm", "ascending": True}])
    rows = [r for r in rows if (r.get("vaalikausitunnus") or config.TERM_ID) == config.TERM_ID]
    n = write_jsonl(raw / "matters.jsonl", rows)
    log.info("matters: %d", n)


# ----------------------------------------------------------------------- MPs
def _served_in_term(mp: dict) -> bool:
    for t in mp.get("edustajatoimet") or []:
        end = t.get("loppupvm")
        if end is None or end >= config.TERM_START:
            return True
    return False


def collect_mps(c: Client, raw: Path) -> None:
    """Full profile of every MP who served in the term (incl. substitutes and leavers).
    IDs come from ballots + speeches + the roster endpoint, then one GET per MP."""
    ids: set[str] = set()
    for f in (raw / "votes").glob("*-*.json"):
        for v in _load_json(f):
            for b in v.get("aanestystapahtumat") or []:
                if b.get("henkilonumero"):
                    ids.add(str(b["henkilonumero"]))
    if (raw / "speeches.jsonl").exists():
        for s in read_jsonl(raw / "speeches.jsonl"):
            s = unwrap(s, "puheenvuoro")
            hn = (s.get("puhuja") or {}).get("henkilonro")
            if hn and str(hn).isdigit():
                ids.add(str(hn))
    roster = c.get_json("kansanedustajat") or {}
    for mp in roster.get("kansanedustajat", roster if isinstance(roster, list) else []):
        if _served_in_term(mp):
            ids.add(str(mp["henkilonro"]))
    for i, hn in enumerate(sorted(ids, key=int), 1):
        path = raw / "mps" / f"{hn}.json"
        data = c.get_json(f"kansanedustajat/{hn}")
        if data is not None:
            _save_json(path, data)
        if i % 50 == 0:
            log.info("MPs: %d/%d", i, len(ids))
    log.info("MPs: %d profiles", len(ids))


# -------------------------------------------------------------------- events
def collect_events(c: Client, raw: Path) -> None:
    try:
        _save_json(raw / "events.json", c.get_json("tapahtumat"))
    except Exception as e:  # non-critical
        log.warning("events skipped: %s", e)


# ------------------------------------------------- official absence statistics
def _dw_fetch(c: Client, chart_id: str) -> tuple[str, str, str] | None:
    """Return (version_url, html, csv_text) for a Datawrapper chart."""
    r = c.s.get(f"https://datawrapper.dwcdn.net/{chart_id}/", timeout=60)
    r.encoding = "utf-8"
    ver = parsers.dw_version_url(chart_id, r.text, r.url)
    if not ver:
        log.warning("Datawrapper %s: could not resolve version", chart_id)
        return None
    h = c.s.get(ver, timeout=60)
    h.encoding = "utf-8"
    d = c.s.get(ver + "dataset.csv", timeout=60)
    d.encoding = "utf-8"
    return ver, h.text, d.text


def collect_official_absences(c: Client, raw: Path) -> None:
    out = raw / "official"
    out.mkdir(parents=True, exist_ok=True)
    for name, cid in [("plenary_totals", config.DW_PLENARY_TOTALS), ("plenary_daily", config.DW_PLENARY_DAILY)]:
        got = _dw_fetch(c, cid)
        if got:
            (out / f"{name}.csv").write_text(got[2], encoding="utf-8")
            log.info("official %s: %d rows", name, got[2].count("\n"))

    # committees: crawl every year x committee table starting from the seed chart
    seen, queue, frames = set(), [config.DW_COMMITTEE_SEED], []
    while queue and len(seen) < 300:
        cid = queue.pop(0)
        if cid in seen:
            continue
        seen.add(cid)
        got = _dw_fetch(c, cid)
        if not got:
            continue
        ver, html, csv_text = got
        m = parsers.COMMITTEE_TITLE_RE.search(parsers.dw_title(html))
        if not m:
            continue
        year, committee = m.group(1), m.group(2)
        df = pd.read_csv(io.StringIO(csv_text), dtype=str)
        df.insert(0, "committee", committee)
        df.insert(0, "year", year)
        df["source_chart"] = ver
        frames.append(df)
        queue.extend(x for x in parsers.dw_links(html) if x not in seen)
    if frames:
        pd.concat(frames, ignore_index=True).to_csv(out / "committee.csv", index=False)
    log.info("official committee absences: %d tables", len(frames))


STEPS = {
    "reference": collect_reference,
    "votes": collect_votes,
    "rollcalls": collect_rollcalls,
    "speeches": collect_speeches,
    "matters": collect_matters,
    "mps": collect_mps,          # after votes + speeches (uses their MP ids)
    "events": collect_events,
    "official": collect_official_absences,
}
