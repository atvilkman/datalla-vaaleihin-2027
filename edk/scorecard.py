"""Scorecard data: per-MP counts (numerators and denominators), roles, calculated fees,
and group/committee facts. The site pools these counts for every level
(Parliament, government/opposition, group, committee, district, MP), so one
formula serves all levels: sum of numerators / sum of denominators.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)
DAYS_PER_MONTH = 30.4375
QUESTION_TYPES = {"Kirjallinen kysymys", "Suullinen kysymys"}
MOTION_TYPES = {"Lakialoite", "Talousarvioaloite", "Lisätalousarvioaloite", "Toimenpidealoite", "Keskustelualoite"}
SPEAKER_BODY = "Puhemiehistö"
SPEAKER_ROLES = {"Puhemies": "speaker", "Ensimmäinen varapuhemies": "deputy", "Toinen varapuhemies": "deputy"}
CONFIDENCE_RE = re.compile(r"^Luottamuslause valtioneuvostolle", re.I)


def _read(t: Path, name: str) -> pd.DataFrame:
    p = t / f"{name}.csv"
    return pd.read_csv(p, dtype=str, keep_default_na=False) if p.exists() else pd.DataFrame()


def _d(s: str | None) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _col(df: pd.DataFrame, *names: str) -> pd.Series:
    for n in names:
        if n in df:
            return df[n].fillna("")
    return pd.Series([""] * len(df), index=df.index)


class Rates:
    """Rate lookup by day from site/fees.json."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        for k in ("mp_fee", "speaker_fee", "chair_supplement", "minister_fee", "allowance"):
            cfg[k] = sorted(cfg[k], key=lambda r: r["from"])

    def at(self, key: str, day: date) -> dict:
        rows = [r for r in self.cfg[key] if r["from"] <= day.isoformat()]
        return rows[-1] if rows else self.cfg[key][0]


def _intervals(df: pd.DataFrame, term_start: date, today: date) -> list[tuple[date, date]]:
    out = []
    for r in df.itertuples():
        s, e = _d(r.start) or term_start, _d(r.end) or today
        if e >= term_start:
            out.append((max(s, term_start), min(e, today)))
    return out


def _months(iv: list[tuple[date, date]]) -> float:
    return sum((e - s).days + 1 for s, e in iv) / DAYS_PER_MONTH


def _in(iv, day: date) -> bool:
    return any(s <= day <= e for s, e in iv)


def build(data_dir: Path, fees_path: Path, term_start: str = "2023-04-05") -> dict:
    t = data_dir / "tables"
    votes, ballots = _read(t, "votes"), _read(t, "ballots")
    mps_t, terms_t = _read(t, "mps"), _read(t, "mp_terms")
    mins_t, mem_t = _read(t, "mp_minister_roles"), _read(t, "mp_memberships")
    ra, rl, rs = _read(t, "rollcall_absences"), _read(t, "rollcall_late_arrivals"), _read(t, "rollcall_sessions")
    sp_t, mt_t, oc_t = _read(t, "speeches"), _read(t, "matters"), _read(t, "official_committee_absences")
    rates = Rates(json.loads(Path(fees_path).read_text(encoding="utf-8")))
    ts = date.fromisoformat(term_start)

    votes = votes[votes.cancelled.str.lower() != "true"]
    b = ballots[ballots.vote_id.isin(votes.vote_id)].copy()
    b["code"] = b.vote.map({"yes": "y", "no": "n", "blank": "b", "absent": "a"}).fillna("a")
    fv_all, lv_all = b.date.min(), b.date.max()
    today = max(_d(lv_all) or date.today(), _d(rs.date.max()) if not rs.empty else ts)
    party_name = b.drop_duplicates("party_abbr").set_index("party_abbr").party.to_dict()

    # --- group majorities and the government bloc -------------------------------------
    cast = b[b.code != "a"]
    cnt = cast.groupby(["vote_id", "party_abbr", "code"]).size().unstack("code", fill_value=0)
    for c in "ynb":
        if c not in cnt:
            cnt[c] = 0
    cnt = cnt[list("ynb")]
    tot, top = cnt.sum(axis=1), cnt.max(axis=1)
    tie = cnt.eq(top, axis=0).sum(axis=1) > 1
    pos = pd.DataFrame({"maj": cnt.idxmax(axis=1).where(~tie & (tot >= 2)), "c": top / tot}).reset_index()

    conf_ids = set(votes.vote_id[votes.title.fillna("").str.match(CONFIDENCE_RE)])
    cp = pos[pos.vote_id.isin(conf_ids) & pos.maj.notna()]
    gov_share = cp.assign(y=cp.maj == "y").groupby("party_abbr").y.mean()
    gov_groups = sorted(p for p, v in gov_share.items() if v >= 0.5)

    b = b.merge(pos[["vote_id", "party_abbr", "maj"]], on=["vote_id", "party_abbr"], how="left")
    gb = b[b.party_abbr.isin(gov_groups) & (b.code != "a")].groupby(["vote_id", "code"]).size().unstack(fill_value=0)
    gov_maj = gb.idxmax(axis=1) if not gb.empty else pd.Series(dtype=str)
    b["gov"] = b.vote_id.map(gov_maj)

    b["lb"] = (b.code != "a") & b.maj.notna()
    b["ag"] = b.lb & (b.code != b.maj)
    b["gbd"] = (b.code != "a") & b.gov.notna()
    b["gbn"] = b.gbd & (b.code == b.gov)
    b["cfd"] = b.vote_id.isin(conf_ids) & (b.code != "a")
    b["cfy"] = b.cfd & (b.code == "y")

    g = b.sort_values("date").groupby("mp_id")
    per = pd.DataFrame({
        "E": g.size(), "C": g.code.apply(lambda s: int((s != "a").sum())),
        "AGd": g.lb.sum(), "AGn": g.ag.sum(), "GBd": g.gbd.sum(), "GBn": g.gbn.sum(),
        "CFd": g.cfd.sum(), "CFy": g.cfy.sum(), "fv": g.date.min(), "lv": g.date.max(),
        "p": g.party_abbr.last(), "d": g.district.last(), "s": g.gender.last(), "name": g.name.last(),
        "ph": g.party_abbr.apply(lambda s: [x for x in dict.fromkeys(s) if x]),
    })

    # --- roll calls ---------------------------------------------------------------------
    rc_dates = sorted(rs.date) if not rs.empty else []
    for key, reason in (("Rw", "parliamentary_work"), ("Rp", "personal"), ("Rn", "other")):
        per[key] = ra[(ra.mp_id != "") & (ra.reason == reason)].groupby("mp_id").size() if not ra.empty else 0
    per["L"] = rl[rl.mp_id != ""].groupby("mp_id").size() if not rl.empty else 0

    def rc_in_office(row):
        s = term_start if row.fv <= fv_all else row.fv
        e = "9999" if row.lv >= lv_all else row.lv
        return sum(1 for d in rc_dates if s <= d <= e)
    per["Rt"] = per.apply(rc_in_office, axis=1)

    # --- speeches, questions, motions ---------------------------------------------------
    if not sp_t.empty:
        sp = sp_t[sp_t.mp_id != ""].copy()
        sp["m"] = pd.to_numeric(sp.duration_min, errors="coerce").fillna(0).clip(lower=0, upper=180)
        per["S"] = sp.groupby("mp_id").size()
        per["Smin"] = sp.groupby("mp_id").m.sum().round(1)
        for code in "TVRNE":
            per["S" + code] = sp[sp.speech_type_code == code].groupby("mp_id").size()
    if not mt_t.empty:
        who = _col(mt_t, "first_signer_mp_id")
        typ = _col(mt_t, "type")
        per["Q"] = mt_t[typ.isin(QUESTION_TYPES) & (who != "")].groupby(who).size()
        per["MO"] = mt_t[typ.isin(MOTION_TYPES) & (who != "")].groupby(who).size()
    for c in ["Rw", "Rp", "Rn", "L", "S", "Smin", "ST", "SV", "SR", "SN", "SE", "Q", "MO"]:
        per[c] = per[c].fillna(0) if c in per else 0

    # --- committee attendance (official statistics) --------------------------------------
    com_att = {}
    if not oc_t.empty:
        oc = oc_t[oc_t.mp_id != ""].copy()
        for c in ("meetings", "total", "personal", "other_no_reason"):
            oc[c] = pd.to_numeric(oc[c], errors="coerce").fillna(0)
        per["Mc"] = oc.groupby("mp_id").meetings.sum()
        per["Ac"] = oc.groupby("mp_id").total.sum()
        per["Acn"] = oc.groupby("mp_id").other_no_reason.sum()
        oc["code"] = oc.committee.str.upper() + "01"
        for code, grp in oc.groupby("code"):
            com_att[code] = {"meetings": int(grp.meetings.sum()), "absent": int(grp.total.sum()),
                             "absent_no_reason": int(grp.other_no_reason.sum())}
    for c in ("Mc", "Ac", "Acn"):
        per[c] = per[c].fillna(0) if c in per else 0

    # --- roles, tenure, fees --------------------------------------------------------------
    info = mps_t.drop_duplicates("mp_id", keep="last").set_index("mp_id") if not mps_t.empty else pd.DataFrame()
    mem = mem_t.copy()
    btype = _col(mem, "body_type")
    # rows differ by type: committees (rooli, valiokuntaNimi), other bodies (rooli, nimi),
    # group roles (tehtava, nimi) - take whichever column is filled on each row
    role = _col(mem, "rooli").where(_col(mem, "rooli") != "", _col(mem, "tehtava"))
    body = _col(mem, "valiokuntaNimi").where(_col(mem, "valiokuntaNimi") != "", _col(mem, "nimi"))
    ccode = _col(mem, "valiokuntaTunnus")
    # group sizes per month (for the group-chair supplement)
    b["ym"] = b.date.str[:7]
    sizes = b.groupby(["ym", "party"]).mp_id.nunique().to_dict()
    months_sorted = sorted({k[0] for k in sizes})

    def group_size(ym: str, party: str) -> int:
        prev = [m for m in months_sorted if m <= ym] or months_sorted[:1]
        for m in reversed(prev):
            if (m, party) in sizes:
                return sizes[(m, party)]
        return 0

    committees, mp_committees = {}, {}
    mp_rows = []
    cap = set(rates.cfg["capital_region"])
    major = set(rates.cfg["major_committees"])
    for r in per.reset_index().itertuples():
        mid = r.mp_id
        i = info.loc[mid] if mid in info.index else None
        tm = terms_t[terms_t.mp_id == mid] if not terms_t.empty else pd.DataFrame(columns=["start", "end"])
        all_terms = [(_d(x.start) or ts, _d(x.end) or today) for x in tm.itertuples()]
        # office window in the term: from the votes (robust when term end dates are missing)
        o_s = ts if r.fv <= fv_all else _d(r.fv)
        o_e = today if r.lv >= lv_all else _d(r.lv)
        office = [(o_s, o_e)]
        T = _months(office)

        m_me = mem[(mem.mp_id == mid)] if not mem.empty else mem
        def iv_where(mask):
            return _intervals(m_me[mask], ts, today) if not m_me.empty else []
        spk_iv = {"speaker": [], "deputy": []}
        if not m_me.empty:
            for k, v in SPEAKER_ROLES.items():
                spk_iv[v] += iv_where((body[m_me.index] == SPEAKER_BODY) & (role[m_me.index] == k))
        com_mask = btype[m_me.index] == "committee" if not m_me.empty else None
        com_rows = m_me[com_mask] if not m_me.empty else m_me
        chair_iv = []
        cm_list = []
        for x in com_rows.itertuples() if not com_rows.empty else []:
            iv = _intervals(pd.DataFrame([{"start": x.start, "end": x.end}]), ts, today)
            if not iv:
                continue
            code = ccode[x.Index] or body[x.Index]
            rl_ = role[x.Index]
            committees.setdefault(code, {"name": body[x.Index], "members": set(), "chairs": set()})
            committees[code]["members"].add(mid)
            cm_list.append({"code": code, "role": rl_, "start": iv[0][0].isoformat(), "end": iv[0][1].isoformat()})
            if rl_ == "Puheenjohtaja":
                committees[code]["chairs"].add(mid)
                chair_iv.append((iv[0], code in major))
        grp_chair_iv = iv_where((btype[m_me.index] == "party_group_role") & (role[m_me.index] == "Puheenjohtaja")) if not m_me.empty else []
        min_rows = mins_t[mins_t.mp_id == mid] if not mins_t.empty else pd.DataFrame()
        min_iv = [(iv, "pääministeri" in str(getattr(x, "nimi", "")).lower())
                  for x in min_rows.itertuples() for iv in _intervals(pd.DataFrame([{"start": x.start, "end": x.end}]), ts, today)]
        Tm = _months([iv for iv, _ in min_iv])
        Tc = sum(_months([(date.fromisoformat(c["start"]), date.fromisoformat(c["end"]))]) for c in cm_list)
        Tl = _months([iv for iv, _ in chair_iv] + grp_chair_iv + spk_iv["speaker"] + spk_iv["deputy"])
        home = (i.home_municipality if i is not None else "") or ""
        district = re.sub(r" vaalipiiri$", "", r.d or "")

        # daily fee calculation
        parts = {"base": 0.0, "speaker": 0.0, "chair": 0.0, "group_chair": 0.0, "minister": 0.0}
        allow = [0.0, 0.0]
        verified = True
        day = o_s
        while o_s and o_e and day <= o_e:
            dim = (date(day.year + (day.month == 12), day.month % 12 + 1, 1) - date(day.year, day.month, 1)).days
            served = sum(((min(e, day - timedelta(days=1)) - s).days + 1) for s, e in all_terms if s < day)
            yrs = served / 365.25
            mp = rates.at("mp_fee", day)
            base = mp["under4"] if yrs < 4 else (mp["4to11"] if yrs < 12 else mp["12plus"])
            verified &= mp["verified"]
            mini = next((pm for iv, pm in min_iv if iv[0] <= day <= iv[1]), None)
            if _in(spk_iv["speaker"], day) or _in(spk_iv["deputy"], day):
                spf = rates.at("speaker_fee", day)
                verified &= spf["verified"]
                parts["speaker"] += (spf["speaker"] if _in(spk_iv["speaker"], day) else spf["deputy"]) / dim
            elif mini is not None:
                mf = rates.at("minister_fee", day)
                verified &= mf["verified"] and rates.cfg["minister_rule_verified"]
                parts["minister"] += (mf["prime_minister"] if mini else mf["minister"]) / dim
                parts["base"] += base * rates.cfg["minister_share_of_mp_fee"] / dim
            else:
                parts["base"] += base / dim
            cs = rates.at("chair_supplement", day)
            chairs_today = [maj for iv, maj in chair_iv if iv[0] <= day <= iv[1]]
            if chairs_today:
                verified &= cs["verified"]
                parts["chair"] += (cs["major_committee"] if any(chairs_today) else cs["other_committee"]) / dim
            if _in(grp_chair_iv, day):
                n = group_size(day.isoformat()[:7], party_name.get(r.p, ""))
                if n >= 3:
                    verified &= cs["verified"]
                    parts["group_chair"] += (cs["group_16plus"] if n >= 16 else cs["group_3to15"]) / dim
            al = rates.at("allowance", day)
            share = rates.cfg["minister_share_of_allowance"] if mini is not None else 1.0
            if home in cap:
                lo = hi = al["capital_region"]
            elif district in ("Helsingin", "Uudenmaan"):
                lo, hi = al["capital_region"], al["outside"]
            else:
                lo, hi = al["outside"], al["outside_with_second_home"]
            allow[0] += lo * share / dim
            allow[1] += hi * share / dim
            day += timedelta(days=1)
        fee_total = round(sum(parts.values()))
        tenure_now = sum(((min(e, today) - s).days + 1) for s, e in all_terms if s <= today) / 365.25 if all_terms else None
        first_year = min((s.year for s, _ in all_terms), default=None)

        roles = []
        if spk_iv["speaker"]:
            roles.append("speaker")
        if spk_iv["deputy"]:
            roles.append("deputy_speaker")
        if min_iv:
            roles.append("minister")
        if chair_iv:
            roles.append("committee_chair")
        if grp_chair_iv:
            roles.append("group_chair")
        switches = max(0, len([x for x in r.ph if x != "erk"]) - 1)
        mp_rows.append({
            "id": mid,
            "f": ((i.calling_name or str(i.first_names).split(" ")[0]) if i is not None else r.name.split(" ")[0]),
            "l": (i.last_name if i is not None else " ".join(r.name.split(" ")[1:])),
            "p": r.p, "d": district, "s": {"Mies": "M", "Nainen": "N"}.get(r.s, r.s), "home": home,
            "by": int(float(i.birth_year)) if i is not None and i.birth_year else None,
            "first_year": first_year, "tenure": round(tenure_now, 1) if tenure_now else None,
            "roles": roles, "cm": cm_list, "ph": r.ph, "fv": r.fv, "lv": None if r.lv >= lv_all else r.lv,
            "ministries": sorted({str(getattr(x, "nimi", "")) for x in min_rows.itertuples()}) if not min_rows.empty else [],
            "c": {"E": int(r.E), "C": int(r.C), "Rt": int(r.Rt), "Rn": int(r.Rn), "Rp": int(r.Rp), "Rw": int(r.Rw),
                  "L": int(r.L), "Mc": int(r.Mc), "Ac": int(r.Ac), "Acn": int(r.Acn),
                  "T": round(T, 2), "Tm": round(Tm, 2), "Tc": round(Tc, 2), "Tl": round(min(Tl, T), 2), "sw": switches,
                  "AGn": int(r.AGn), "AGd": int(r.AGd), "GBn": int(r.GBn), "GBd": int(r.GBd),
                  "CFy": int(r.CFy), "CFd": int(r.CFd), "S": int(r.S), "Smin": float(r.Smin),
                  "ST": int(r.ST), "SV": int(r.SV), "SR": int(r.SR), "SN": int(r.SN), "SE": int(r.SE),
                  "Q": int(r.Q), "MO": int(r.MO),
                  "fee": fee_total, "feeMin": round(allow[0]), "feeMax": round(allow[1])},
            "fee": {"total": fee_total, "parts": {k: round(v) for k, v in parts.items()},
                    "allow": [round(allow[0]), round(allow[1])], "verified": bool(verified)},
        })
        mp_committees[mid] = [c["code"] for c in cm_list]

    # --- group and committee facts ---------------------------------------------------------
    grp = pos.groupby("party_abbr").agg(n=("c", "size"), unan=("c", lambda s: float((s == 1).mean())))
    groups = {p: {"name": party_name.get(p, p), "bloc": "gov" if p in gov_groups else "opp",
                  "unan": round(100 * r.unan, 1), "votes": int(r.n)} for p, r in zip(grp.index, grp.itertuples())}
    com_out = {}
    for code, c in committees.items():
        chair_fee = sum(next(m for m in mp_rows if m["id"] == x)["fee"]["parts"]["chair"] for x in c["chairs"])
        com_out[code] = {"name": c["name"], "members": sorted(c["members"]), "chairs": sorted(c["chairs"]),
                         "chair_supplements": round(chair_fee), "att": com_att.get(code)}
    meta = {"generated": date.today().isoformat(), "votes": int(len(votes)), "rollcalls": len(rc_dates),
            "firstVote": fv_all, "lastVote": lv_all, "lastRollcall": rc_dates[-1] if rc_dates else None,
            "govGroups": gov_groups, "confidenceVotes": len(conf_ids),
            "feeSources": {k: [dict(x) for x in rates.cfg[k]] for k in ("mp_fee", "speaker_fee", "chair_supplement", "minister_fee", "allowance")},
            "ministerRule": {"verified": rates.cfg["minister_rule_verified"], "source": rates.cfg["minister_rule_source"]}}
    return {"meta": meta, "groups": groups, "committees": com_out, "mps": mp_rows}
