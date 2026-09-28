"""Step 3: derived, voter-facing metrics. Every metric is traceable to raw rows."""
from __future__ import annotations

import itertools
import logging

import pandas as pd

from . import config

log = logging.getLogger(__name__)
CAST = ("yes", "no", "blank")


def party_positions(ballots: pd.DataFrame) -> pd.DataFrame:
    """Majority position of each party group in each vote (among members who voted).
    Ties give no position. Includes cohesion (share voting with the majority)."""
    cast = ballots[ballots.vote.isin(CAST)]
    counts = cast.groupby(["vote_id", "party_abbr", "vote"]).size().unstack("vote", fill_value=0)
    for c in CAST:
        if c not in counts:
            counts[c] = 0
    counts = counts[list(CAST)]
    n = counts.sum(axis=1)
    top = counts.max(axis=1)
    is_tie = counts.eq(top, axis=0).sum(axis=1) > 1
    pos = counts.idxmax(axis=1).where(~is_tie)
    out = pd.DataFrame({"members_voting": n, "majority": pos, "cohesion": (top / n).round(4)})
    return out.reset_index()


def mp_scorecard(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    mps = t.get("mps", pd.DataFrame()).copy()
    votes = t["votes"]
    b = t["ballots"].merge(votes[["vote_id", "cancelled"]], on="vote_id", how="left")
    b = b[~b.cancelled.fillna(False)]

    pp = party_positions(b)
    b = b.merge(pp[["vote_id", "party_abbr", "majority", "members_voting"]], on=["vote_id", "party_abbr"], how="left")
    b["with_party"] = (b.vote == b.majority)
    b["counts_for_loyalty"] = b.vote.isin(CAST) & b.majority.notna() & (b.members_voting >= 2)

    g = b.groupby("mp_id")
    sc = pd.DataFrame({
        "votes_eligible": g.size(),
        "votes_cast": g.vote.apply(lambda s: s.isin(CAST).sum()),
        "votes_absent": g.vote.apply(lambda s: (s == "absent").sum()),
        "votes_yes": g.vote.apply(lambda s: (s == "yes").sum()),
        "votes_no": g.vote.apply(lambda s: (s == "no").sum()),
        "votes_blank": g.vote.apply(lambda s: (s == "blank").sum()),
        "loyalty_base": g.counts_for_loyalty.sum(),
        "votes_against_party": g.apply(lambda x: (x.counts_for_loyalty & ~x.with_party).sum()),
        "first_vote": g.date.min(), "last_vote": g.date.max(),
        "parties_in_votes": g.party_abbr.apply(lambda s: ", ".join(dict.fromkeys(s.dropna()))),
    })
    sc["vote_participation_pct"] = (100 * sc.votes_cast / sc.votes_eligible).round(1)
    sc["party_line_pct"] = (100 * (sc.loyalty_base - sc.votes_against_party) / sc.loyalty_base.where(sc.loyalty_base > 0)).round(1)

    # roll calls (session level; includes absences for parliamentary work, which official stats exclude)
    ra = t.get("rollcall_absences", pd.DataFrame())
    if not ra.empty:
        r = ra.dropna(subset=["mp_id"]).pivot_table(index="mp_id", columns="reason", values="rollcall_id",
                                                    aggfunc="count", fill_value=0)
        r.columns = [f"rollcall_absent_{c}" for c in r.columns]
        sc = sc.join(r, how="outer")
    rl = t.get("rollcall_late_arrivals", pd.DataFrame())
    if not rl.empty:
        rl = rl.dropna(subset=["mp_id"])
        sc = sc.join(rl.groupby("mp_id").agg(late_arrivals=("rollcall_id", "count"),
                                             late_minutes_total=("minutes_late", "sum"),
                                             late_minutes_median=("minutes_late", "median")), how="outer")
    # official statistics
    ot = t.get("official_plenary_absence_totals", pd.DataFrame())
    if not ot.empty:
        sc = sc.join(ot.dropna(subset=["mp_id"]).groupby("mp_id")[["personal", "other_no_reason", "total"]].sum()
                     .add_prefix("official_plenary_"), how="outer")
    oc = t.get("official_committee_absences", pd.DataFrame())
    if not oc.empty:
        sc = sc.join(oc.dropna(subset=["mp_id"]).groupby("mp_id")[["meetings", "personal", "other_no_reason", "total"]].sum()
                     .add_prefix("official_committee_"), how="outer")
        if "official_committee_meetings" in sc:
            sc["official_committee_absence_pct"] = (100 * sc.official_committee_total / sc.official_committee_meetings
                                                    .where(sc.official_committee_meetings > 0)).round(1)
    # speeches
    sp = t.get("speeches", pd.DataFrame())
    if not sp.empty:
        s = sp.dropna(subset=["mp_id"]).groupby("mp_id").agg(speeches=("speech_id", "count"),
                                                            speaking_minutes=("duration_min", "sum"),
                                                            speech_words=("words", "sum"))
        sc = sc.join(s, how="outer")
    # initiatives / questions as first signer
    mt = t.get("matters", pd.DataFrame())
    if not mt.empty and "first_signer_ids" in mt:
        m = mt.assign(mp_id=mt.first_signer_ids.fillna("").str.split("; ")).explode("mp_id")
        m = m[m.mp_id != ""]
        if not m.empty:
            piv = m.pivot_table(index="mp_id", columns="type", values="matter_id", aggfunc="count", fill_value=0)
            piv.columns = [f"first_signed: {c}" for c in piv.columns]
            piv["first_signed_total"] = piv.sum(axis=1)
            sc = sc.join(piv, how="outer")
    # context: time in office during the term, ministerial periods
    if not t.get("mp_terms", pd.DataFrame()).empty:
        tt = t["mp_terms"].copy()
        start = pd.Timestamp(config.TERM_START)
        today = pd.Timestamp.today().normalize()
        tt["s"] = pd.to_datetime(tt.start, errors="coerce").clip(lower=start)
        tt["e"] = pd.to_datetime(tt.end, errors="coerce").fillna(today).clip(upper=today)
        tt = tt[tt.e >= start]
        tt["days"] = (tt.e - tt.s).dt.days.clip(lower=0) + 1
        sc = sc.join(tt.groupby("mp_id").days.sum().rename("days_in_office_this_term"), how="left")
    mr = t.get("mp_minister_roles", pd.DataFrame())
    if not mr.empty:
        mr = mr[pd.to_datetime(mr.end, errors="coerce").fillna(pd.Timestamp.today()) >= pd.Timestamp(config.TERM_START)]
        sc = sc.join(mr.groupby("mp_id").size().rename("minister_periods_this_term"), how="left")
    # roll-call sessions held while the MP was in office -> absence / lateness rates
    rs = t.get("rollcall_sessions", pd.DataFrame())
    if not rs.empty and not t.get("mp_terms", pd.DataFrame()).empty:
        d = pd.to_datetime(rs.date, errors="coerce").dropna()
        held = {}
        for mp_id, grp in t["mp_terms"].groupby("mp_id"):
            mask = pd.Series(False, index=d.index)
            for r in grp.itertuples():
                s0 = pd.to_datetime(r.start, errors="coerce")
                e0 = pd.to_datetime(r.end, errors="coerce")
                mask |= (d >= s0) & (d <= (e0 if pd.notna(e0) else d.max()))
            held[mp_id] = int(mask.sum())
        sc = sc.join(pd.Series(held, name="rollcall_sessions_in_office"), how="left")

    counts = [c for c in sc.columns if c.startswith(("rollcall_absent_", "late_", "speech", "speaking_", "first_signed"))]
    counts += [c for c in ("late_arrivals", "minister_periods_this_term") if c in sc]
    for c in set(counts) - {"late_minutes_median"}:
        sc[c] = sc[c].fillna(0)
    if "rollcall_sessions_in_office" in sc:
        base = sc.rollcall_sessions_in_office.where(sc.rollcall_sessions_in_office > 0)
        for c in [c for c in sc.columns if c.startswith("rollcall_absent_")] + (["late_arrivals"] if "late_arrivals" in sc else []):
            sc[f"{c}_pct"] = (100 * sc[c] / base).round(1)
    sc.index.name = "mp_id"
    sc = sc.reset_index()
    if not mps.empty:
        sc = mps[["mp_id", "name", "latest_party", "latest_district", "gender", "birth_year", "mandate_ended"]].merge(
            sc, on="mp_id", how="right")
    return sc


def rebel_votes(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Every ballot where an MP voted differently from their party group's majority."""
    b = t["ballots"].merge(t["votes"][["vote_id", "cancelled", "title", "matter_ids", "date"]].rename(columns={"date": "vdate"}),
                           on="vote_id")
    b = b[~b.cancelled.fillna(False)]
    pp = party_positions(b)
    b = b.merge(pp, on=["vote_id", "party_abbr"])
    out = b[b.vote.isin(CAST) & b.majority.notna() & (b.members_voting >= 2) & (b.vote != b.majority)]
    return out[["vote_id", "vdate", "title", "matter_ids", "mp_id", "name", "party_abbr", "vote", "majority",
                "members_voting", "cohesion"]].rename(columns={"vdate": "date", "majority": "party_majority"})


def party_cohesion(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    pp = party_positions(t["ballots"])
    return (pp.groupby("party_abbr").agg(votes=("vote_id", "nunique"), mean_cohesion=("cohesion", "mean"),
                                         unanimous_share=("cohesion", lambda s: (s == 1).mean()))
            .round(4).reset_index().sort_values("mean_cohesion", ascending=False))


def party_agreement(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Share of votes in which two party groups' majorities took the same position."""
    pp = party_positions(t["ballots"]).dropna(subset=["majority"])
    wide = pp.pivot(index="vote_id", columns="party_abbr", values="majority")
    rows = []
    for a, b in itertools.product(wide.columns, repeat=2):
        both = wide[[a, b]].dropna()
        rows.append({"party_a": a, "party_b": b, "votes_compared": len(both),
                     "agreement_pct": round(100 * (both[a] == both[b]).mean(), 1) if len(both) else None})
    return pd.DataFrame(rows)


def build_analytics(t: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    if t.get("ballots") is None or t["ballots"].empty:
        log.warning("no ballots -> analytics skipped")
        return {}
    return {"mp_scorecard": mp_scorecard(t), "rebel_votes": rebel_votes(t),
            "party_cohesion": party_cohesion(t), "party_agreement": party_agreement(t)}
