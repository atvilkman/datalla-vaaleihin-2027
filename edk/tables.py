"""Step 2: turn raw downloads into tidy tables (one row per entity)."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pandas as pd

from . import config, parsers
from .client import read_jsonl, unwrap

log = logging.getLogger(__name__)
MATTER_RE = re.compile(r"\b([A-ZÅÄÖ][A-Za-zÅÄÖåäö]{0,6} \d{1,4}/\d{4} vp)\b")


def loc(v, lang: str = config.LANG):
    """Localised field {fi, sv, en} -> string."""
    if isinstance(v, dict):
        return v.get(lang) or v.get("fi") or next((x for x in v.values() if x), None)
    return v


def txt(v):
    """Any value -> readable scalar (localised dicts to text, other structures to JSON)."""
    if isinstance(v, dict) and set(v) & {"fi", "sv", "en"}:
        return loc(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False) if v else None
    return v


def _vote_value(s: str | None) -> str | None:
    s = (s or "").strip().lower()
    if s.startswith("jaa"):
        return "yes"
    if s.startswith("ei"):
        return "no"
    if s.startswith("tyhj"):
        return "blank"
    if s.startswith("poissa"):
        return "absent"
    return s or None


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- votes
def build_votes(raw: Path) -> dict[str, pd.DataFrame]:
    votes, ballots, groups = [], [], []
    for f in sorted((raw / "votes").glob("*-*.json")):
        for v in _load(f):
            kohta = v.get("kohta") or {}
            kohta_txt = json.dumps(kohta, ensure_ascii=False)
            matters = sorted(set(MATTER_RE.findall(kohta_txt + " " + (loc(v.get("aanestysotsikko")) or ""))))
            res = v.get("aanestystulos") or {}
            votes.append({
                "vote_id": v["id"], "session_id": v.get("istunnonTunniste"),
                "date": (v.get("istuntopvm") or "")[:10],
                "vote_start": v.get("aanestysalkuaika"),
                "vote_number": v.get("aanestysnumero"),
                "title": loc(v.get("aanestysotsikko")),
                "agenda_title": loc(v.get("paivajarjestyksenotsikko")),
                "stage": loc(kohta.get("kasittelyotsikkonimi")) if isinstance(kohta, dict) else None,
                "matter_ids": "; ".join(matters),
                "cancelled": bool(v.get("aanestysmitatoity")),
                "speaker_id": (v.get("puhemies") or {}).get("henkilonumero"),
                "yes": res.get("jaa"), "no": res.get("ei"), "blank": res.get("tyhjia"),
                "absent": res.get("poissa"), "total": res.get("yhteensa"),
                "kohta_json": kohta_txt,
            })
            for b in v.get("aanestystapahtumat") or []:
                ballots.append({
                    "vote_id": v["id"], "date": (v.get("istuntopvm") or "")[:10],
                    "mp_id": str(b.get("henkilonumero")),
                    "name": f"{b.get('etunimi', '')} {b.get('sukunimi', '')}".strip(),
                    "party_abbr": loc(b.get("edkryhmalyhenne")),
                    "party": loc(b.get("eduskuntaryhma")),
                    "district": loc(b.get("vaalipiiri")),
                    "gender": loc(b.get("sukupuoli")),
                    "vote": _vote_value(loc(b.get("kayttaytyminen"))),
                    "vote_raw": loc(b.get("kayttaytyminen")),
                })
            for gtype, key in [("party", "eduskuntaryhmaJakaumat"),
                               ("government_opposition", "hallitusoppositioJakaumat"),
                               ("district", "vaalipiiriJakaumat")]:
                for g in v.get(key) or []:
                    groups.append({"vote_id": v["id"], "group_type": gtype, "group": loc(g.get("nimi")),
                                   "yes": g.get("jaa"), "no": g.get("ei"), "blank": g.get("tyhjia"),
                                   "absent": g.get("poissa"), "total": g.get("yhteensa")})
    return {"votes": pd.DataFrame(votes), "ballots": pd.DataFrame(ballots),
            "vote_group_results": pd.DataFrame(groups)}


# ---------------------------------------------------------------- roll calls
def build_rollcalls(raw: Path) -> dict[str, pd.DataFrame]:
    idx_path = raw / "rollcalls" / "_index.json"
    index = {d["edktunnus"]: d for d in (_load(idx_path) if idx_path.exists() else [])}
    sessions, people = [], []
    for f in sorted((raw / "rollcalls").glob("*.html")):
        edk = f.stem
        parsed = parsers.parse_rollcall(f.read_text(encoding="utf-8"))
        meta = index.get(edk, {})
        date = parsed["session_date"] or meta.get("laadintapvm")
        sec = {s["kind"]: s for s in parsed["sections"]}
        sessions.append({
            "rollcall_id": edk, "date": date, "session_start": parsed["session_start"],
            "title": meta.get("nimeketeksti"),
            "absent_expected": (sec.get("absent") or {}).get("expected"),
            "absent_parsed": (sec.get("absent") or {}).get("parsed", 0),
            "late_expected": (sec.get("late_arrival") or {}).get("expected"),
            "late_parsed": (sec.get("late_arrival") or {}).get("parsed", 0),
        })
        for p in parsed["people"]:
            people.append({"rollcall_id": edk, "date": date, "session_start": parsed["session_start"], **p})
    s = pd.DataFrame(sessions)
    ppl = pd.DataFrame(people)
    if not s.empty:
        bad = s[(s.absent_expected.notna() & (s.absent_expected != s.absent_parsed)) |
                (s.late_expected.notna() & (s.late_expected != s.late_parsed))]
        if len(bad):
            log.warning("roll calls: %d reports where parsed count != stated count", len(bad))
    absences = ppl[ppl.kind == "absent"].drop(columns=["kind", "arrival_time", "minutes_late"], errors="ignore") if not ppl.empty else ppl
    late = ppl[ppl.kind == "late_arrival"].drop(columns=["kind", "reason", "reason_code"], errors="ignore") if not ppl.empty else ppl
    return {"rollcall_sessions": s, "rollcall_absences": absences, "rollcall_late_arrivals": late}


# ------------------------------------------------------------------ speeches
def build_speeches(raw: Path) -> dict[str, pd.DataFrame]:
    p = raw / "speeches.jsonl"
    if not p.exists():
        return {}
    rows = []
    for s in read_jsonl(p):
        s = unwrap(s, "puheenvuoro")
        sp = s.get("puhuja") or {}
        text = loc(s.get("puheenvuoro")) or ""
        asia = s.get("asia")
        start, end = s.get("aloitushetki"), s.get("lopetushetki")
        rows.append({
            "speech_id": s.get("id"), "code": s.get("tunnus"),
            "date": (start or "")[:10], "start": start, "end": end,
            "session_year": s.get("valtiopaivavuosi"), "session_no": s.get("taysistuntonumero"),
            "mp_id": str(sp.get("henkilonro")) if sp.get("henkilonro") else None,
            "name": f"{sp.get('etunimi', '')} {sp.get('sukunimi', '')}".strip(),
            "role": loc(sp.get("asema")), "party_code": sp.get("eduskuntaryhma_tunnus"),
            "speech_type": loc(s.get("puheenvuorotyyppinimi")),
            "speech_type_code": s.get("puheenvuorotyyppikoodi"),
            "matter": "; ".join(sorted(set(MATTER_RE.findall(json.dumps(asia, ensure_ascii=False))))) if asia else None,
            "matter_title": loc(asia.get("nimeke")) if isinstance(asia, dict) else None,
            "words": len(text.split()), "text": text,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        st = pd.to_datetime(df.start, errors="coerce", utc=True, format="ISO8601")
        en = pd.to_datetime(df.end, errors="coerce", utc=True, format="ISO8601")
        df["duration_min"] = ((en - st).dt.total_seconds() / 60).round(2)
    return {"speeches": df}


# ------------------------------------------------------------------- matters
def _person_ids(obj) -> list[str]:
    txt = json.dumps(obj, ensure_ascii=False)
    return sorted(set(re.findall(r'"henkilo(?:nro|numero)"\s*:\s*"?(\d+)', txt)))


def build_matters(raw: Path) -> dict[str, pd.DataFrame]:
    p = raw / "matters.jsonl"
    if not p.exists():
        return {}
    rows, keywords = [], []
    for m in read_jsonl(p):
        m = unwrap(m, "valtiopaivaasia")
        first = m.get("ensimmainenAllekirjoittaja")
        ids = _person_ids(first)
        rows.append({
            "matter_id": m.get("eduskuntatunnus"), "type": txt(m.get("asiakirjatyyppinimi")),
            "type_code": m.get("asiakirjatyyppikoodi"), "title": txt(m.get("nimeke")),
            "status": txt(m.get("tila")), "decision": txt(m.get("kokonaispaatosnimi")),
            "latest_stage": txt(m.get("viimeisinKasittelyvaihe")),
            "created": m.get("laadintapvm"), "closed": m.get("paattymispvm"),
            "first_signer_ids": "; ".join(ids),
            "first_signer_raw": json.dumps(first, ensure_ascii=False) if first else None,
            "other_signers": m.get("muidenAllekirjoittajienLkm"),
            "session_year": m.get("valtiopaivavuosi"),
        })
        for k in m.get("asiasanat") or []:
            if isinstance(k, dict):
                k = next((k[x] for x in ("asiasana", "nimi", "teksti") if x in k), k)
            keywords.append({"matter_id": m.get("eduskuntatunnus"), "keyword": txt(k)})
    return {"matters": pd.DataFrame(rows), "matter_keywords": pd.DataFrame(keywords)}


# ----------------------------------------------------------------------- MPs
def _periods(items, mp_id, extra=lambda x: {}):
    # some lists arrive per language: {"fi": [...], "sv": [...], "en": [...]} (verified live)
    if isinstance(items, dict) and set(items) & {"fi", "sv", "en"}:
        items = items.get(config.LANG) or items.get("fi") or []
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        out.append({"mp_id": mp_id, "start": it.get("alkupvm"), "end": it.get("loppupvm"),
                    **extra(it)})
    return out


def _flat(it: dict) -> dict:
    """Generic flattening for nested objects whose exact schema we don't rely on."""
    out = {}
    for k, v in it.items():
        if k in ("alkupvm", "loppupvm"):
            continue
        out[k] = txt(v)
    return out


def build_mps(raw: Path) -> dict[str, pd.DataFrame]:
    mps, parties, commit, interests, minister, breaks, subs, edu, career, terms = ([] for _ in range(10))
    for f in sorted((raw / "mps").glob("*.json"), key=lambda p: int(p.stem)):
        m = _load(f)
        mp_id = str(m.get("henkilonro"))
        lastg = m.get("viimeisinEduskuntaryhma")
        mps.append({
            "mp_id": mp_id, "first_names": m.get("etunimet"), "calling_name": m.get("kutsumanimi"),
            "last_name": m.get("sukunimi"),
            "name": f"{m.get('kutsumanimi') or (m.get('etunimet') or '').split(' ')[0]} {m.get('sukunimi')}",
            "birth_year": m.get("syntymavuosi"), "birth_place": m.get("syntymapaikka"),
            "home_municipality": loc(m.get("kotikunta")), "gender": loc(m.get("sukupuolikoodi")),
            "profession": loc(m.get("ammatti")), "email": m.get("sahkoposti"),
            "status": txt(m.get("edustajantoimenTila")),
            "mandate_ended": m.get("kansanedustajuusPaattynytPvm"),
            "latest_party": txt(lastg.get("nimi")) if isinstance(lastg, dict) and "nimi" in lastg else txt(lastg),
            "latest_district": txt((m.get("viimeisinVaalipiiri") or {}).get("nimi")) if isinstance(m.get("viimeisinVaalipiiri"), dict) and "nimi" in m["viimeisinVaalipiiri"] else txt(m.get("viimeisinVaalipiiri")),
            "current_minister": txt(m.get("nykyinenMinisteriys")),
        })
        terms += _periods(m.get("edustajatoimet"), mp_id)
        parties += _periods(m.get("eduskuntaryhmat"), mp_id, lambda x: {"party": loc(x.get("nimi")), "party_code": x.get("tunnus")})
        commit += _periods(m.get("valiokuntajasenyydet"), mp_id, _flat)
        commit += [dict(r, body_type="other_body") for r in _periods(m.get("toimielinjasenyydet"), mp_id, _flat)]
        minister += _periods(m.get("valtioneuvostonJasenyydet"), mp_id, _flat)
        breaks += _periods(m.get("edustajatoimiKeskeytynyt"), mp_id, _flat)
        subs += _periods(m.get("edustajatoimiSijaisuus"), mp_id, _flat)
        edu += [{"mp_id": mp_id, **_flat(x)} for x in (m.get("koulutukset") or []) if isinstance(x, dict)]
        career += [{"mp_id": mp_id, **_flat(x)} for x in (m.get("tyoura") or []) if isinstance(x, dict)]
        sid = m.get("sidonnaisuudet") or {}
        for it in (loc(sid) if isinstance(sid, dict) else sid) or []:
            interests.append({"mp_id": mp_id, "year": it.get("vuosi"), "category": it.get("ryhmaotsikko"),
                              "declaration": it.get("sidonta"), "declaration_type": it.get("ilmoitusTyyppi"),
                              "order": it.get("jarjestys")})
        for g in m.get("eduskuntaryhmaTehtavat") or []:
            commit.append({"mp_id": mp_id, "start": (g or {}).get("alkupvm"), "end": (g or {}).get("loppupvm"),
                           "body_type": "party_group_role", **_flat(g or {})})
    return {"mps": pd.DataFrame(mps), "mp_terms": pd.DataFrame(terms),
            "mp_party_history": pd.DataFrame(parties), "mp_memberships": pd.DataFrame(commit),
            "mp_interests": pd.DataFrame(interests), "mp_minister_roles": pd.DataFrame(minister),
            "mp_mandate_breaks": pd.DataFrame(breaks), "mp_substitutions": pd.DataFrame(subs),
            "mp_education": pd.DataFrame(edu), "mp_career": pd.DataFrame(career)}


# ------------------------------------------------- official absence statistics
def name_index(mps: pd.DataFrame) -> dict[str, str]:
    """name key -> mp_id, trying 'First Last', 'Last First' with calling and first given name."""
    idx, clash = {}, set()
    for r in mps.itertuples():
        firsts = {r.calling_name or "", (r.first_names or "").split(" ")[0]}
        for fn in filter(None, firsts):
            for key in (f"{fn} {r.last_name}", f"{r.last_name} {fn}"):
                k = parsers.norm_name(key)
                if k in idx and idx[k] != r.mp_id:
                    clash.add(k)
                idx[k] = r.mp_id
    for k in clash:
        idx.pop(k, None)
    return idx


def attach_ids(df: pd.DataFrame, col: str, idx: dict[str, str], label: str) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df.insert(0, "mp_id", df[col].map(lambda s: idx.get(parsers.norm_name(str(s)))))
    miss = df[df.mp_id.isna()][col].drop_duplicates()
    if len(miss):
        log.warning("%s: %d names not matched to an MP id, e.g. %s", label, len(miss), list(miss[:5]))
    return df


def build_official(raw: Path, mps: pd.DataFrame) -> dict[str, pd.DataFrame]:
    o, out = raw / "official", {}
    idx = name_index(mps) if not mps.empty else {}
    if (o / "plenary_totals.csv").exists():
        t = pd.read_csv(o / "plenary_totals.csv")
        t = t.rename(columns={"Kansanedustaja": "name", "Henkilökohtainen syy": "personal",
                              "Muu poissaolo": "other_no_reason", "Yhteensä": "total"})
        out["official_plenary_absence_totals"] = attach_ids(t, "name", idx, "official totals")
    if (o / "plenary_daily.csv").exists():
        d = pd.read_csv(o / "plenary_daily.csv", dtype=str)
        d = d.rename(columns={"Istuntopäivä": "date", "Kansanedustaja": "name",
                              "Eduskuntaryhmä": "party", "Poissaolon syy": "reason"})
        d["date"] = d["date"].map(parsers.parse_fi_date)
        out["official_plenary_absence_daily"] = attach_ids(d, "name", idx, "official daily")
    if (o / "committee.csv").exists():
        c = pd.read_csv(o / "committee.csv", dtype=str)
        c = c.rename(columns={"Kansanedustaja": "name", "Rooli": "role", "Mistä lähtien": "from",
                              "Mihin asti": "to", "Kokoukset": "meetings_range", "Kokouksia": "meetings",
                              "Henkilökohtainen syy": "personal", "Muu poissaolo": "other_no_reason",
                              "Yhteensä": "total"})
        c["role_changed"] = c["name"].str.contains(r"\*", regex=True)
        for col in ("meetings", "personal", "other_no_reason", "total"):
            c[col] = pd.to_numeric(c[col], errors="coerce")
        out["official_committee_absences"] = attach_ids(c, "name", idx, "official committee")
    return out


def build_all(raw: Path) -> dict[str, pd.DataFrame]:
    tables: dict[str, pd.DataFrame] = {}
    for fn in (build_votes, build_rollcalls, build_speeches, build_matters, build_mps):
        try:
            tables.update(fn(raw))
        except FileNotFoundError as e:
            log.warning("%s skipped: %s", fn.__name__, e)
    mps = tables.get("mps", pd.DataFrame())
    idx = name_index(mps) if not mps.empty else {}
    for key in ("rollcall_absences", "rollcall_late_arrivals"):
        if key in tables and not tables[key].empty:
            tables[key] = attach_ids(tables[key], "name", idx, key)
    tables.update(build_official(raw, mps))
    # reference tables
    for f in sorted((raw / "reference").glob("*.json")):
        data = _load(f)
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [data])
        try:
            tables[f"ref_{f.stem}"] = pd.json_normalize(data)
        except Exception:
            pass
    return tables
