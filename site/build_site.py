"""Build the public site (one static HTML page) from the pipeline's tables.

    python site/build_site.py --data data --out public/index.html

Metric definitions match the method section on the page and the v1 snapshot.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
TERM_START = "2023-04-05"
KEY_VOTE_RE = re.compile(r"^Luottamuslause valtioneuvostolle", re.I)
MIN_GROUP_VOTES = 50   # groups with fewer votes (e.g. short-lived) are left out of group charts


def _read(tables: Path, name: str) -> pd.DataFrame:
    p = tables / f"{name}.csv"
    return pd.read_csv(p, dtype=str, keep_default_na=False) if p.exists() else pd.DataFrame()


def _agenda(kohta_json: str, fallback: str) -> str:
    try:
        k = json.loads(kohta_json) if kohta_json else {}
    except ValueError:
        k = {}
    o = k.get("otsikko") if isinstance(k, dict) else None
    text = (o.get("fi") if isinstance(o, dict) else o) or fallback or ""
    text = re.split(r"\s{2,}", text.strip())[0]
    return text[:140].rsplit(" ", 1)[0] + "…" if len(text) > 140 else text


def build(data_dir: Path) -> dict:
    t = data_dir / "tables"
    votes, ballots = _read(t, "votes"), _read(t, "ballots")
    mps_t, mins_t = _read(t, "mps"), _read(t, "mp_minister_roles")
    ra, rl, rs = _read(t, "rollcall_absences"), _read(t, "rollcall_late_arrivals"), _read(t, "rollcall_sessions")
    if votes.empty or ballots.empty:
        raise SystemExit("No votes/ballots in tables – run the pipeline first.")

    votes = votes[votes.cancelled.str.lower() != "true"]
    b = ballots[ballots.vote_id.isin(votes.vote_id)].copy()
    b["code"] = b.vote.map({"yes": "y", "no": "n", "blank": "b", "absent": "a"}).fillna("a")
    fv_all, lv_all = b.date.min(), b.date.max()

    # party positions per vote
    cast = b[b.code != "a"]
    cnt = cast.groupby(["vote_id", "party_abbr", "code"]).size().unstack("code", fill_value=0)
    for c in "ynb":
        if c not in cnt:
            cnt[c] = 0
    cnt = cnt[list("ynb")]
    tot, top = cnt.sum(axis=1), cnt.max(axis=1)
    tie = cnt.eq(top, axis=0).sum(axis=1) > 1
    pos = pd.DataFrame({"maj": cnt.idxmax(axis=1).where(~tie & (tot >= 2)), "c": top / tot}).reset_index()

    b = b.merge(pos[["vote_id", "party_abbr", "maj"]], on=["vote_id", "party_abbr"], how="left")
    b["lb"] = (b.code != "a") & b.maj.notna()
    b["ag"] = b.lb & (b.code != b.maj)

    g = b.sort_values("date").groupby("mp_id")
    per = pd.DataFrame({
        "e": g.size(), "y": g.code.apply(lambda s: (s == "y").sum()), "n": g.code.apply(lambda s: (s == "n").sum()),
        "b": g.code.apply(lambda s: (s == "b").sum()), "a": g.code.apply(lambda s: (s == "a").sum()),
        "lb": g.lb.sum(), "ag": g.ag.sum(), "fv": g.date.min(), "lv": g.date.max(),
        "p": g.party_abbr.last(), "d": g.district.last(), "s": g.gender.last(), "name": g.name.last(),
        "ph": g.party_abbr.apply(lambda s: list(s.value_counts().index)),
    })

    # roll calls
    reasons = {"parliamentary_work": "rw", "personal": "rp", "other": "ro"}
    if not ra.empty:
        r = ra[ra.mp_id != ""].groupby(["mp_id", "reason"]).size().unstack(fill_value=0)
        for k, v in reasons.items():
            per[v] = r[k] if k in r else 0
    if not rl.empty:
        rl = rl[rl.mp_id != ""].copy()
        rl["m"] = pd.to_numeric(rl.minutes_late, errors="coerce").fillna(0)
        per["lt"] = rl.groupby("mp_id").size()
        per["lm"] = rl.groupby("mp_id").m.sum()
    for c in ["rw", "rp", "ro", "lt", "lm"]:
        per[c] = per[c].fillna(0).astype(int) if c in per else 0
    rc_dates = sorted(rs.date) if not rs.empty else []

    def in_office(row):
        s = TERM_START if row.fv <= fv_all else row.fv
        e = "9999" if row.lv >= lv_all else row.lv
        return sum(1 for d in rc_dates if s <= d <= e)
    per["io"] = per.apply(in_office, axis=1)

    # one row per MP (the same person can be saved twice if the API answers under a normalised id)
    info = mps_t.drop_duplicates("mp_id", keep="last").set_index("mp_id") if not mps_t.empty else pd.DataFrame()
    mins = {}
    for r_ in mins_t.itertuples() if not mins_t.empty else []:
        if (r_.end or "9999") >= "2023-06-20" and (r_.start or "") >= "2023-01-01":
            mins.setdefault(r_.mp_id, []).append([getattr(r_, "nimi", "") or "ministeri", r_.start, r_.end or ""])

    per = per.reset_index().sort_values(["p", "name"])
    mps = []
    for r_ in per.itertuples():
        i = info.loc[r_.mp_id] if r_.mp_id in info.index else None
        first = (i.calling_name or i.first_names.split(" ")[0]) if i is not None else r_.name.split(" ")[0]
        last = i.last_name if i is not None else " ".join(r_.name.split(" ")[1:])
        by = int(float(i.birth_year)) if i is not None and i.birth_year else None
        mps.append(dict(id=r_.mp_id, f=first, l=last, p=r_.p, d=re.sub(r" vaalipiiri$", "", r_.d or ""),
                        s={"Mies": "M", "Nainen": "N"}.get(r_.s, r_.s), by=by,
                        e=int(r_.e), y=int(r_.y), n=int(r_.n), b=int(r_.b), a=int(r_.a), lb=int(r_.lb), ag=int(r_.ag),
                        rw=int(r_.rw), rp=int(r_.rp), ro=int(r_.ro), lt=int(r_.lt), lm=int(r_.lm), io=int(r_.io),
                        min=sorted(mins.get(r_.mp_id, []), key=lambda x: x[1]),
                        ph=r_.ph if len(r_.ph) > 1 else [], fv=r_.fv, lv=None if r_.lv >= lv_all else r_.lv))
    order = [m["id"] for m in mps]

    # key votes: confidence in the government
    kv = votes[votes.title.fillna("").str.match(KEY_VOTE_RE)].sort_values(["date", "vote_id"])
    code_by = b.set_index(["vote_id", "mp_id"]).code
    key = []
    for v in kv.itertuples():
        codes = code_by.get(v.vote_id)
        m = codes.to_dict() if codes is not None else {}
        key.append(dict(id=v.vote_id, date=v.date, title=v.title, agenda=_agenda(v.kohta_json, v.title),
                        r=[int(float(v.yes or 0)), int(float(v.no or 0)), int(float(v.blank or 0)), int(float(v.absent or 0))],
                        v="".join(m.get(i, "-") for i in order)))

    # groups
    grp = pos.groupby("party_abbr").agg(c_mean=("c", "mean"), c_unan=("c", lambda s: (s == 1).mean()), c_n=("c", "size"))
    coh = [[p, round(100 * float(r_.c_mean), 1), round(100 * float(r_.c_unan), 1), int(r_.c_n)]
           for p, r_ in zip(grp.index, grp.itertuples()) if r_.c_n > MIN_GROUP_VOTES]
    wide = pos.dropna(subset=["maj"]).pivot(index="vote_id", columns="party_abbr", values="maj")
    agree = []
    for a in [c[0] for c in coh]:
        for c2 in [c[0] for c in coh]:
            if a in wide and c2 in wide:
                x, y = wide[a], wide[c2]          # two Series (wide[[a, a]] would duplicate the column)
                mask = x.notna() & y.notna()
                n = int(mask.sum())
                agree.append([a, c2, round(100 * float((x[mask] == y[mask]).mean()), 1) if n else None, n])
            else:
                agree.append([a, c2, None, 0])

    sp = votes.speaker_id[votes.speaker_id.str.fullmatch(r"\d+")] if "speaker_id" in votes else pd.Series(dtype=str)
    speakers = [sp.mode().iloc[0]] if len(sp) else []
    meta = dict(generated=date.today().isoformat(), votes=int(len(votes)), rollcalls=len(rc_dates),
                firstVote=fv_all, lastVote=lv_all, lastRollcall=rc_dates[-1] if rc_dates else None, speakers=speakers)
    return dict(meta=meta, mps=mps, key=key, coh=coh, agree=agree)


def _json_default(o):
    """numpy scalars -> Python; anything else is a bug worth a clear message."""
    if hasattr(o, "item") and not hasattr(o, "__len__"):
        return o.item()
    raise TypeError(f"Cannot put {type(o).__name__} into site data: {str(o)[:200]}")


def _css() -> str:
    tpl = (HERE / "template.html").read_text(encoding="utf-8")
    return tpl[tpl.index("<style>") + 7: tpl.index("</style>")]


def build_extra_pages(data_dir: Path, out_dir: Path, json_dir: Path | None = None) -> dict:
    """The scorecard (tuloskortti.html) and the explanation page (mittaristo.html)."""
    import sys
    sys.path.insert(0, str(HERE.parent))
    from edk import scorecard
    sc = scorecard.build(data_dir, HERE / "fees.json", TERM_START)
    kpi = (HERE / "kpi_defs.js").read_text(encoding="utf-8")
    css = _css()
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"), default=_json_default).replace("</", "<\\/")
    card = (HERE / "scorecard.html").read_text(encoding="utf-8")
    card = card.replace("__CSS__", css).replace("__KPI__", kpi).replace("__DATA__", dump(sc))
    method = (HERE / "method.html").read_text(encoding="utf-8")
    method = method.replace("__CSS__", css).replace("__KPI__", kpi).replace("__FEES__", dump({"feeSources": sc["meta"]["feeSources"], "ministerRule": sc["meta"]["ministerRule"]}))
    (out_dir / "tuloskortti.html").write_text(card, encoding="utf-8")
    (out_dir / "mittaristo.html").write_text(method, encoding="utf-8")
    if json_dir:
        json_dir.mkdir(parents=True, exist_ok=True)
        (json_dir / "scorecard.json").write_text(json.dumps(sc, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
    return sc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="public/index.html")
    ap.add_argument("--json", default=None, help="also write the site data as JSON (audit trail)")
    a = ap.parse_args()
    d = build(Path(a.data))
    html = (HERE / "template.html").read_text(encoding="utf-8").replace(
        "__DATA__", json.dumps(d, ensure_ascii=False, separators=(",", ":"), default=_json_default))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(d, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
    print(f"site: {out} ({len(html)//1024} kB) – {len(d['mps'])} MPs, {d['meta']['votes']} votes, {len(d['key'])} key votes")
    sc = build_extra_pages(Path(a.data), out.parent, Path(a.json).parent if a.json else None)
    print(f"scorecard: {len(sc['mps'])} MPs, {len(sc['groups'])} groups, {len(sc['committees'])} committees")


if __name__ == "__main__":
    main()
