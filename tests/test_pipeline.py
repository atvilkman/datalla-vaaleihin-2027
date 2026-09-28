"""Offline tests. Fixtures mirror the shapes observed on the live API (Sept 2026)."""
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from edk import analytics, parsers, tables  # noqa: E402
from edk.client import RateLimiter, unwrap  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def test_rollcall_parser():
    r = parsers.parse_rollcall((FIX / "rollcall_sample.html").read_text(encoding="utf-8"))
    assert r["session_date"] == "2026-09-24" and r["session_start"] == "10:00"
    ab = [p for p in r["people"] if p["kind"] == "absent"]
    late = [p for p in r["people"] if p["kind"] == "late_arrival"]
    assert [p["reason"] for p in ab] == ["parliamentary_work", "personal", "other"]
    assert ab[2]["name"] == "Pihla Keto-Huovinen" and ab[2]["party_abbr"] == "kok"
    assert [(p["arrival_time"], p["minutes_late"]) for p in late] == [("10:15", 15), ("10:24", 24)]
    assert all(s["expected"] == s["parsed"] for s in r["sections"])


def test_datawrapper_helpers():
    base = '<meta http-equiv="REFRESH" content="0; url=https://datawrapper.dwcdn.net/xfYFQ/14/">'
    assert parsers.dw_version_url("xfYFQ", base) == "https://datawrapper.dwcdn.net/xfYFQ/14/"
    html = ('<meta property="og:title" content="Poissaolot 2025 (SuV)">'
            '<a href="https://datawrapper.dwcdn.net/vetCa">2024</a> "https:\\/\\/datawrapper.dwcdn.net\\/XvcxW"')
    assert parsers.COMMITTEE_TITLE_RE.search(parsers.dw_title(html)).groups() == ("2025", "SuV")
    assert parsers.dw_links(html) == ["XvcxW", "vetCa"]
    assert parsers.parse_fi_date("16.05.2023@@16.05.2023") == "2023-05-16"


def test_unwrap_and_limiter():
    assert unwrap({"type": "Aanestys", "aanestys": {"id": 1}}, "aanestys") == {"id": 1}
    assert unwrap({"id": 1}, "aanestys") == {"id": 1}
    rl = RateLimiter(3, 100)
    for _ in range(3):
        rl.wait()
    assert len(rl.calls) == 3


def _ballot(hn, first, last, party, vote):
    return {"vaalipiiri": {"fi": "Helsingin vaalipiiri"}, "eduskuntaryhma": {"fi": f"{party} ryhmä"},
            "sukupuoli": {"fi": "Mies"}, "kayttaytyminen": {"fi": vote}, "henkilonumero": hn,
            "sukunimi": last, "etunimi": first, "edkryhmalyhenne": {"fi": party}}


def _mp(hn, first, last, party, start="2023-04-05", end=None):
    return {"henkilonro": hn, "etunimet": first, "kutsumanimi": first, "sukunimi": last,
            "syntymavuosi": 1980, "sukupuolikoodi": "Mies", "ammatti": {"fi": "insinööri"},
            "viimeisinEduskuntaryhma": {"nimi": {"fi": f"{party} ryhmä"}},
            "edustajatoimet": [{"alkupvm": start, "loppupvm": end}],
            "eduskuntaryhmat": [{"nimi": {"fi": f"{party} ryhmä"}, "tunnus": party.upper(), "alkupvm": start, "loppupvm": end}],
            "sidonnaisuudet": {"fi": [{"sidonta": "Hallituksen jäsen, Oy X", "ryhmaotsikko": "Luottamustehtävät",
                                       "vuosi": "2026", "ilmoitusTyyppi": "sidonnaisuusilmoitus", "jarjestys": 1}]},
            "valtioneuvostonJasenyydet": {"fi": [{"alkupvm": "2023-06-20", "loppupvm": None, "nimi": "valtiovarainministeri"}]} if hn == "1" else {"fi": []},
            "valiokuntajasenyydet": [{"alkupvm": "2023-06-28", "loppupvm": None, "nimi": {"fi": "Suuri valiokunta"}, "rooli": {"fi": "jäsen"}}]}


def make_raw(raw: Path):
    (raw / "votes").mkdir(parents=True)
    (raw / "mps").mkdir()
    (raw / "rollcalls").mkdir()
    (raw / "official").mkdir()
    people = [("1", "Anders", "Adlercreutz", "r"), ("2", "Tuula", "Väätäinen", "sd"), ("3", "Ville", "Skinnari", "sd"),
              ("4", "Pihla", "Keto-Huovinen", "kok"), ("5", "Pauli", "Aalto-Setälä", "kok")]
    votes = []
    patterns = [["Jaa", "Ei", "Ei", "Jaa", "Jaa"], ["Poissa", "Jaa", "Ei", "Jaa", "Ei"], ["Jaa", "Ei", "Ei", "Ei", "Ei"]]
    for i, pat in enumerate(patterns, 1):
        votes.append({"id": f"2026-85-{i}", "istunnonTunniste": "2026-85", "istuntopvm": "2026-09-24+03:00",
                      "aanestysotsikko": {"fi": "Luottamuslause valtioneuvostolle / Testi ehdotus" if i == 1 else f"Hyväksyminen HE 12/2026 vp äänestys {i}"},
                      "puhemies": {"henkilonumero": "5"}, "aanestysnumero": str(i),
                      "aanestysmitatoity": i == 3, "kohta": {"kasittelyotsikkonimi": {"fi": "Ainoa käsittely"}, "otsikko": {"fi": "Välikysymys testiasiasta"}},
                      "aanestystulos": {"jaa": 3, "ei": 2, "tyhjia": 0, "poissa": 0, "yhteensa": 5},
                      "eduskuntaryhmaJakaumat": [{"nimi": {"fi": "sd ryhmä"}, "jaa": 0, "ei": 2, "tyhjia": 0, "poissa": 0, "yhteensa": 2}],
                      "aanestystapahtumat": [_ballot(hn, f, l, p, v) for (hn, f, l, p), v in zip(people, pat)]})
    (raw / "votes" / "2026-85.json").write_text(json.dumps(votes, ensure_ascii=False), encoding="utf-8")
    for hn, f, l, p in people:
        (raw / "mps" / f"{hn}.json").write_text(json.dumps(_mp(hn, f, l, p), ensure_ascii=False), encoding="utf-8")
    shutil.copy(FIX / "rollcall_sample.html", raw / "rollcalls" / "EDK-2026-AK-47692.html")
    (raw / "rollcalls" / "_index.json").write_text(json.dumps([{"edktunnus": "EDK-2026-AK-47692", "laadintapvm": "2026-09-24"}]))
    with open(raw / "speeches.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "Puheenvuoro", "puheenvuoro": {"id": "s1", "aloitushetki": "2026-09-24T10:30:00+03:00",
            "lopetushetki": "2026-09-24T10:35:30+03:00", "puhuja": {"henkilonro": "3", "etunimi": "Ville", "sukunimi": "Skinnari"},
            "puheenvuorotyyppinimi": {"fi": "Varsinainen puheenvuoro"}, "puheenvuoro": {"fi": "Arvoisa puhemies, tämä on testi."},
            "asia": {"eduskuntatunnus": "HE 12/2026 vp", "nimeke": {"fi": "Testilaki"}}}}, ensure_ascii=False) + "\n")
    with open(raw / "matters.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"eduskuntatunnus": "LA 5/2026 vp", "asiakirjatyyppinimi": "Lakialoite", "nimeke": "Laki X",
                            "vaalikausitunnus": "2023-2026", "ensimmainenAllekirjoittaja": [{"henkilonro": "2", "sukunimi": "Väätäinen"}],
                            "muidenAllekirjoittajienLkm": 12, "asiasanat": [{"asiasana": {"fi": "vero"}}]}, ensure_ascii=False) + "\n")
    (raw / "official" / "plenary_totals.csv").write_text(
        '"Kansanedustaja","Henkilökohtainen syy","Muu poissaolo","Yhteensä"\n"Tuula Väätäinen",6,20,26\n"Pihla Keto-Huovinen",1,2,3\n', encoding="utf-8")
    (raw / "official" / "plenary_daily.csv").write_text(
        '"Istuntopäivä","Kansanedustaja","Eduskuntaryhmä","Poissaolon syy"\n"16.05.2023@@16.05.2023","Tuula Väätäinen","sd","Henkilökohtainen syy"\n', encoding="utf-8")
    (raw / "official" / "committee.csv").write_text(
        "year,committee,Kansanedustaja,Rooli,Mistä lähtien,Mihin asti,Kokoukset,Kokouksia,Henkilökohtainen syy,Muu poissaolo,Yhteensä,source_chart\n"
        "2026,SuV,Adlercreutz Anders*,pj,29.04.2026,-,21 /2026 vp,21,0,1,1,x\n", encoding="utf-8")


def test_end_to_end(tmp_path):
    raw = tmp_path / "raw"
    make_raw(raw)
    t = tables.build_all(raw)
    assert len(t["votes"]) == 3 and len(t["ballots"]) == 15
    assert set(t["rollcall_absences"].reason) == {"parliamentary_work", "personal", "other"}
    assert t["rollcall_absences"].mp_id.notna().all() and t["rollcall_late_arrivals"].mp_id.notna().all()
    assert t["official_committee_absences"].mp_id.iloc[0] == "1"
    assert t["official_plenary_absence_daily"].date.iloc[0] == "2023-05-16"
    assert t["speeches"].duration_min.iloc[0] == 5.5
    assert t["matters"].first_signer_ids.iloc[0] == "2"
    a = analytics.build_analytics(t)
    sc = a["mp_scorecard"].set_index("mp_id")
    # vote 3 is cancelled -> 2 eligible votes each
    assert (sc.votes_eligible == 2).all()
    assert sc.loc["1", "votes_absent"] == 1 and sc.loc["1", "vote_participation_pct"] == 50.0
    # kok (4,5): vote 2 split 1-1 -> tie -> no majority; vote 1 both Jaa -> loyal
    assert sc.loc["5", "votes_against_party"] == 0
    # sd (2,3): vote 1 both Ei; vote 2 Jaa vs Ei -> tie -> excluded
    assert sc.loc["2", "loyalty_base"] == 1
    assert sc.loc["3", "late_arrivals"] == 1 and sc.loc["3", "speeches"] == 1
    assert sc.loc["2", "first_signed_total"] == 1
    assert sc.loc["1", "minister_periods_this_term"] == 1
    assert {"party_a", "party_b", "agreement_pct"} <= set(a["party_agreement"].columns)


class _Resp:
    def __init__(self, status, payload=None, text="", url=""):
        self.status_code, self._p, self.text, self.url, self.encoding = status, payload, text, url, "utf-8"

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_client_paging_and_dataset(monkeypatch):
    from edk import client as cl
    monkeypatch.setattr(cl.config, "GET_DELAY_S", 0)
    c = cl.Client()
    calls = []

    def fake_request(method, url, timeout=None, json=None, **kw):
        calls.append((method, url, json))
        if url.endswith("search/count"):
            return _Resp(200, {"count": 2500 if json["category"] == "aanestys" else 9000})
        if url.endswith("/search"):
            start = json["startFromIndex"]
            n = min(1000, 2500 - start)
            return _Resp(200, {"results": [{"type": "Aanestys", "aanestys": {"id": start + i}} for i in range(n)]})
        if url.endswith("search/dataset"):
            return _Resp(202, {"jobId": "j1"})
        if "dataset/status" in url:
            return _Resp(200, {"status": "COMPLETED", "resultUrl": "https://s3/x.ndjson"})
        raise AssertionError(url)

    monkeypatch.setattr(c.s, "request", fake_request)
    monkeypatch.setattr(c.s, "get", lambda url, timeout=None: _Resp(200, text='{"puheenvuoro":{"id":"a","puheenvuoro":{"fi":"x"}}}\n{"id":"b"}\n'))
    rows = c.search_all("aanestys", {"property": "istuntopvm", "fromDate": "2023-04-05", "toDate": None}, sort=[])
    assert len(rows) == 2500 and rows[-1]["id"] == 2499
    rows = c.search_all("puheenvuoro", {}, sort=[])
    assert [r["id"] for r in rows] == ["a", "b"] and rows[0]["puheenvuoro"] == {"fi": "x"}
    assert all("User-Agent" in c.s.headers for _ in [0])


def test_site_build(tmp_path):
    import importlib.util, subprocess
    raw = tmp_path / "raw"
    make_raw(raw)
    subprocess.run([sys.executable, str(ROOT / "run.py"), "--no-collect", "--out", str(tmp_path)], check=True, capture_output=True)
    spec = importlib.util.spec_from_file_location("build_site", ROOT / "site" / "build_site.py")
    bs = importlib.util.module_from_spec(spec); spec.loader.exec_module(bs)
    d = bs.build(tmp_path)
    assert d["meta"]["votes"] == 2 and d["meta"]["speakers"] == ["5"]
    assert len(d["key"]) == 1 and d["key"][0]["agenda"] == "Välikysymys testiasiasta"
    assert len(d["key"][0]["v"]) == len(d["mps"]) == 5
    m = {x["id"]: x for x in d["mps"]}
    assert m["1"]["min"][0][0] == "valtiovarainministeri"
    assert m["1"]["a"] == 1 and m["1"]["rw"] == 1 and m["5"]["lt"] == 1 and m["5"]["lm"] == 24
    assert m["4"]["ro"] == 1 and m["2"]["rp"] == 1
    assert m["1"]["io"] == 1
