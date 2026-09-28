"""A larger synthetic parliament (shapes as seen on the live API) to exercise every scorecard path."""
import json
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from edk import scorecard  # noqa: E402

GROUPS = {"aa": "Alfa ryhmä", "bb": "Beta ryhmä", "cc": "Gamma ryhmä"}
GOV = {"aa", "bb"}


def _loc(s):
    return {"fi": s, "sv": s, "en": s}


def rollcall_html(day: date, absent, late):
    def person(first, last, party, extra):
        x = f'<span class="LisatietoTeksti">({extra})&nbsp;</span>' if extra else ""
        return (f'<span class="Toimija"><span class="Henkilo"><span class="EtuNimi">{first}&nbsp;</span>'
                f'<span class="SukuNimi">{last}&nbsp;</span><span class="LisatietoTeksti">{party}&nbsp;</span>{x}</span></span>')
    head = f"<h1>Nimenhuutoraportti tiistai {day.day}.{day.month}.{day.year} klo 14.00</h1>"
    a = "".join(person(f, l, p, c) for f, l, p, c in absent)
    t = "".join(person(f, l, p, c) for f, l, p, c in late)
    return (f"<html><body><main>{head}<p class=\"edk-KappaleKooste\">Nimenhuudossa merkittiin täysistunnosta poissa oleviksi "
            f"seuraavat {len(absent)} edustajaa:</p><div class=\"OsallistujaOsa\">{a}</div>"
            f"<p class=\"edk-KappaleKooste\">Nimenhuudon jälkeen ilmoittautuivat seuraavat {len(late)} edustajaa:</p>"
            f"<div class=\"OsallistujaOsa\">{t}</div></main></body></html>")


def make_parliament(raw: Path, n=24, seed=7):
    rnd = random.Random(seed)
    for d in ("votes", "mps", "rollcalls", "official", "reference"):
        (raw / d).mkdir(parents=True, exist_ok=True)
    people = []
    for i in range(n):
        grp = list(GROUPS)[i % 3]
        people.append({"id": str(100 + i), "f": f"Etu{i}", "l": f"Suku{i}", "p": grp,
                       "d": ["Helsingin vaalipiiri", "Uudenmaan vaalipiiri", "Oulun vaalipiiri"][i % 3],
                       "home": ["Helsinki", "Kerava", "Oulu"][i % 3]})
    # MP profiles: old terms for some, a minister, a speaker, committee chairs, a group chair
    for i, m in enumerate(people):
        terms = [{"alkupvm": "2023-04-05", "loppupvm": None}]
        if i % 4 == 0:
            terms.insert(0, {"alkupvm": "2011-04-20", "loppupvm": "2023-04-04"})
        prof = {"henkilonro": m["id"], "etunimet": m["f"], "kutsumanimi": m["f"], "sukunimi": m["l"],
                "syntymavuosi": 1970 + i, "sukupuolikoodi": "Nainen" if i % 2 else "Mies", "kotikunta": m["home"],
                "viimeisinEduskuntaryhma": {"nimi": _loc(GROUPS[m["p"]])},
                "edustajatoimet": terms,
                "eduskuntaryhmat": [{"nimi": _loc(GROUPS[m["p"]]), "tunnus": m["p"], "alkupvm": "2023-04-05", "loppupvm": None}],
                "valiokuntajasenyydet": [
                    {"rooli": _loc("Puheenjohtaja" if i in (3, 4) else "Jäsen"), "valiokuntaTunnus": "SUV01" if i % 2 else "TUV01",
                     "valiokuntaNimi": _loc("Suuri valiokunta" if i % 2 else "Tulevaisuusvaliokunta"),
                     "alkupvm": "2023-06-28", "loppupvm": None}],
                "toimielinjasenyydet": {"fi": ([{"nimi": "Puhemiehistö", "rooli": "Puhemies", "alkupvm": "2023-06-21", "loppupvm": None}] if i == 1 else [])
                                        + ([{"nimi": "Puhemiehistö", "rooli": "Toinen varapuhemies", "alkupvm": "2023-06-21", "loppupvm": None}] if i == 2 else [])},
                "valtioneuvostonJasenyydet": {"fi": [{"alkupvm": "2023-06-20", "loppupvm": None, "nimi": "pääministeri" if i == 0 else "valtiovarainministeri"}]} if i in (0, 6) else {"fi": []},
                "eduskuntaryhmaTehtavat": [{"alkupvm": "2023-06-20", "loppupvm": None, "nimi": _loc(GROUPS[m["p"]]), "tehtava": _loc("Puheenjohtaja")}] if i == 5 else []}
        (raw / "mps" / f"{m['id']}.json").write_text(json.dumps(prof, ensure_ascii=False), encoding="utf-8")
    # votes: 40 over 2023-06..2026-09, every 5th a confidence vote
    votes, day = [], date(2023, 6, 20)
    for k in range(40):
        conf = k % 5 == 0
        ballots = []
        for i, m in enumerate(people):
            if i == 1:          # the Speaker does not vote
                v = "Poissa"
            elif rnd.random() < 0.08:
                v = "Poissa"
            elif m["p"] in GOV:
                v = "Jaa" if (conf or rnd.random() < 0.95) else "Ei"
            else:
                v = "Ei" if (conf or rnd.random() < 0.9) else "Jaa"
            ballots.append({"vaalipiiri": _loc(m["d"]), "eduskuntaryhma": _loc(GROUPS[m["p"]]), "sukupuoli": _loc("Mies"),
                            "kayttaytyminen": _loc(v), "henkilonumero": m["id"], "sukunimi": m["l"], "etunimi": m["f"],
                            "edkryhmalyhenne": _loc(m["p"])})
        votes.append({"id": f"{day.year}-{k}-1", "istunnonTunniste": f"{day.year}-{k}", "istuntopvm": day.isoformat() + "+03:00",
                      "istuntovpvuosi": str(day.year),
                      "aanestysotsikko": _loc("Luottamuslause valtioneuvostolle / X ehdotus" if conf else f"Äänestys {k}"),
                      "kohta": {"otsikko": _loc(f"Välikysymys {k}")}, "aanestysmitatoity": False,
                      "puhemies": {"henkilonumero": "101"},
                      "aanestystulos": {"jaa": 1, "ei": 1, "tyhjia": 0, "poissa": 0, "yhteensa": 2},
                      "aanestystapahtumat": ballots})
        day += timedelta(days=29)
    (raw / "votes" / "all-all.json").write_text(json.dumps(votes, ensure_ascii=False), encoding="utf-8")
    # roll calls
    index = []
    for k in range(12):
        dd = date(2023, 9, 5) + timedelta(days=90 * k)
        absent = [(people[j]["f"], people[j]["l"], people[j]["p"], rnd.choice(["e", "h", ""])) for j in rnd.sample(range(n), 3)]
        late = [(people[j]["f"], people[j]["l"], people[j]["p"], "14.25") for j in rnd.sample(range(n), 2)]
        edk = f"EDK-{k}"
        (raw / "rollcalls" / f"{edk}.html").write_text(rollcall_html(dd, absent, late), encoding="utf-8")
        index.append({"edktunnus": edk, "laadintapvm": dd.isoformat()})
    (raw / "rollcalls" / "_index.json").write_text(json.dumps(index))
    # speeches and matters
    with open(raw / "speeches.jsonl", "w", encoding="utf-8") as f:
        for k in range(60):
            m = people[rnd.randrange(n)]
            st = date(2024, 1, 10) + timedelta(days=k * 7)
            f.write(json.dumps({"id": f"s{k}", "aloitushetki": f"{st}T12:00:00+02:00", "lopetushetki": f"{st}T12:0{k % 9}:30+02:00",
                                "puhuja": {"henkilonro": m["id"], "etunimi": m["f"], "sukunimi": m["l"]},
                                "puheenvuorotyyppikoodi": rnd.choice("TVRN"), "puheenvuorotyyppinimi": _loc("x"),
                                "puheenvuoro": _loc("Arvoisa puhemies")}, ensure_ascii=False) + "\n")
    with open(raw / "matters.jsonl", "w", encoding="utf-8") as f:
        for k in range(30):
            m = people[rnd.randrange(n)]
            f.write(json.dumps({"eduskuntatunnus": f"X {k}/2024 vp", "vaalikausitunnus": "2023-2026",
                                "asiakirjatyyppinimi": _loc(rnd.choice(["Lakialoite", "Kirjallinen kysymys", "Toimenpidealoite"])),
                                "ensimmainenAllekirjoittaja": {"fi": [{"rooli": "Ensimmäinen allekirjoittaja", "etunimi": m["f"],
                                                                       "sukunimi": m["l"], "eduskuntaryhma": m["p"]}]}},
                               ensure_ascii=False) + "\n")
    rows = "year,committee,Kansanedustaja,Rooli,Mistä lähtien,Mihin asti,Kokoukset,Kokouksia,Henkilökohtainen syy,Muu poissaolo,Yhteensä,source_chart\n"
    for i, m in enumerate(people):
        rows += f"2025,{'SuV' if i % 2 else 'TuV'},{m['l']} {m['f']},jäsen,28.06.2023,-,x,40,{i % 3},{i % 2},{i % 3 + i % 2},x\n"
    (raw / "official" / "committee.csv").write_text(rows, encoding="utf-8")
    return people


def test_scorecard_synthetic(tmp_path):
    make_parliament(tmp_path / "raw")
    subprocess.run([sys.executable, str(ROOT / "run.py"), "--no-collect", "--out", str(tmp_path)], check=True, capture_output=True)
    d = scorecard.build(tmp_path, ROOT / "site" / "fees.json")
    json.dumps(d)                                   # serialisable
    assert d["meta"]["govGroups"] == ["aa", "bb"] and d["meta"]["confidenceVotes"] == 8
    mp = {m["id"]: m for m in d["mps"]}
    assert len(mp) == 24
    assert "speaker" in mp["101"]["roles"] and "deputy_speaker" in mp["102"]["roles"]
    assert "minister" in mp["100"]["roles"] and "committee_chair" in mp["103"]["roles"] and "group_chair" in mp["105"]["roles"]
    # Speaker's fee replaces the MP fee; prime minister gets minister fee + half MP fee
    assert mp["101"]["fee"]["parts"]["speaker"] > 0 and mp["101"]["fee"]["parts"]["base"] < 3 * 7137   # MP fee only before 21.6.2023
    assert mp["100"]["fee"]["parts"]["minister"] > mp["106"]["fee"]["parts"]["minister"] > 0
    assert 0 < mp["100"]["fee"]["parts"]["base"] < mp["107"]["fee"]["parts"]["base"]
    # 12+ years (terms since 2011) -> higher base than a first-term MP with the same days
    assert mp["104"]["fee"]["parts"]["base"] > mp["107"]["fee"]["parts"]["base"]
    assert mp["103"]["fee"]["parts"]["chair"] > 0 and mp["104"]["fee"]["parts"]["chair"] > 0
    assert mp["105"]["fee"]["parts"]["group_chair"] > 0
    # allowance: Helsinki home -> fixed, Oulu -> range
    lo, hi = mp["100"]["fee"]["allow"]
    assert lo == hi
    lo, hi = mp["102"]["fee"]["allow"]
    assert hi > lo
    assert all(m["c"]["C"] <= m["c"]["E"] and m["c"]["AGn"] <= m["c"]["AGd"] for m in d["mps"])
    assert sum(m["c"]["Q"] + m["c"]["MO"] for m in d["mps"]) == 30
    assert sum(m["c"]["S"] for m in d["mps"]) == 60
    assert sum(m["c"]["Rn"] + m["c"]["Rp"] + m["c"]["Rw"] for m in d["mps"]) == 36
    assert sum(m["c"]["L"] for m in d["mps"]) == 24
    assert d["committees"]["SUV01"]["name"] == "Suuri valiokunta" and d["committees"]["SUV01"]["att"]["meetings"] == 480
    assert d["groups"]["cc"]["bloc"] == "opp"
    assert mp["101"]["fee"]["verified"] is False     # pre-October-2025 amounts are unverified


def test_pages_build(tmp_path):
    make_parliament(tmp_path / "raw")
    subprocess.run([sys.executable, str(ROOT / "run.py"), "--no-collect", "--out", str(tmp_path)], check=True, capture_output=True)
    r = subprocess.run([sys.executable, str(ROOT / "site" / "build_site.py"), "--data", str(tmp_path),
                        "--out", str(tmp_path / "public" / "index.html"), "--json", str(tmp_path / "sd" / "latest.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for page in ("index.html", "tuloskortti.html", "mittaristo.html"):
        html = (tmp_path / "public" / page).read_text(encoding="utf-8")
        assert "__DATA__" not in html and "__KPI__" not in html and "__CSS__" not in html and "__FEES__" not in html
    assert (tmp_path / "sd" / "scorecard.json").exists()
