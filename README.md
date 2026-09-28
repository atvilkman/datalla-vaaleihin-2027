# Data-based Voting 2027 – Eduskunta data pipeline and site

Collects **all openly available data on the 2023– parliamentary term** (5 Apr 2023 → today),
turns it into tidy tables and computes voter-facing metrics per MP and party.
Every number can be traced back to an official source row.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# put your contact e-mail in the User-Agent (required by the API; polite)
python run.py --user-agent "DataBasedVoting2027/1.0 (contact: you@yourdomain.fi)" --parquet
```

First run: roughly 20–40 min (≈1,500 GET requests, a few rate-limited POSTs, one bulk speech export).
Re-runs reuse the cache in `data/raw/` and only fetch what is new – run it daily (cron / Task Scheduler).

```bash
python run.py --steps votes rollcalls official   # refresh selected sources
python run.py --no-collect                       # rebuild tables/analytics from cache only
python -m pytest tests                           # offline tests
```

## Publish for free (GitHub Pages)

Everything below is free for a public repository: code hosting, Actions minutes and Pages hosting.
The site will be at `https://<your-username>.github.io/<repo-name>/`.

1. Create a GitHub account and a **public** repository, e.g. `datalla-vaaleihin-2027`.
2. Upload the contents of this folder (keep the folder structure, including `.github/`).
3. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
4. **Settings → Secrets and variables → Actions → Variables → New variable:** `CONTACT_EMAIL` = your e-mail
   (sent to Eduskunta in the User-Agent so they can contact you instead of blocking the traffic).
5. **Actions → refresh-and-publish → Run workflow.** The first run downloads the whole term
   (roughly 30–60 minutes); later runs use the cache and take a few minutes.

After that the site updates automatically every morning and after plenary sessions (Tue–Fri).
Each run also commits `site-data/latest.json`, a dated copy of every number on the site (audit trail).
If a run fails, the previous version of the site stays online.

Notes:
- GitHub can pause scheduled workflows in repos with no activity for 60 days. The daily data commit
  normally keeps the repo active; if it is ever paused, GitHub e-mails you and one click re-enables it.
- No custom domain is needed. One can be added later under Settings → Pages without changing anything else.

## The site

| Page | File | Content |
|---|---|---|
| Front page | `index.html` (from `site/template.html`) | Confidence votes on the chamber diagram, MPs by district, MP profiles, group agreement |
| Scorecard | `tuloskortti.html` (from `site/scorecard.html`) | Six sections (presence, roles, voting line, contribution, consistency & transparency, costs) at six levels (Parliament, government/opposition, group, committee, district, MP), cost lens, highest/lowest lists with filters |
| How the scorecard works | `mittaristo.html` (from `site/method.html`) | Why, how to read, every measure's card (what it tells / doesn't tell), comparison rules, costs and fee sources, what we don't show, the company analogy |

- Measure definitions live in one place: `site/kpi_defs.js` (formulas, applicability, texts in Finnish and English).
- Scorecard numbers are computed by `edk/scorecard.py` as per-MP counts; the page pools them for any level (sum of numerators / sum of denominators).
- Fee rules live in `site/fees.json` with effective dates, sources and a `verified` flag. When the fee committee decides new amounts, or an amount is confirmed from an official source, update this file; the site marks every figure that depends on unverified amounts.
- Each build also writes `site-data/scorecard.json` (committed daily as an audit trail).

## Sources

| Source | What | How |
|---|---|---|
| `api.eduskunta.fi` votes | Every plenary vote + every MP's ballot, party / gov-opposition / district breakdowns | `POST /search` on `istuntovpvuosi`, full records 100 per page |
| `api.eduskunta.fi` roll-call reports (*Nimenhuutoraportti*, NHR) | Per session: absent MPs **with reason** (parliamentary work / personal / no reason) and **late arrivals with registration time** | search NHR docs → HTML parsed |
| `api.eduskunta.fi` speeches | All speeches, full text, speaker, type, timing, matter | `/search/dataset` bulk job |
| `api.eduskunta.fi` matters | Govt bills, MPs' motions, written questions, citizens' initiatives, interpellations, budget motions; first signer, co-signer count, keywords, outcome | search `valtiopaivaasia` |
| `api.eduskunta.fi` MPs | Profile, party history (switches), committees, ministerial posts, **declared interests**, mandate breaks, substitutes, education, career | `GET /kansanedustajat/{id}` |
| Official absence statistics (eduskunta.fi, Datawrapper CSV) | Plenary absences per MP (totals + per day, personal vs. no reason), **committee absences per committee per year** | CSV download, crawler follows all year × committee tables |
| Reference data, events | Parties, districts, committees, speech types … | `reference-data/*`, `tapahtumat` |

## Output

`data/raw/` – untouched downloads (the audit trail). `data/tables/` – tidy tables. `data/analytics/` – metrics.
CSV is UTF-8 with BOM (opens correctly in Excel); `--parquet` adds Parquet.

### tables/
| Table | Grain | Key columns |
|---|---|---|
| `votes` | vote | vote_id, session_id, date, title, stage, matter_ids, cancelled, yes/no/blank/absent |
| `ballots` | MP × vote | vote_id, mp_id, name, party_abbr, district, gender, vote (`yes/no/blank/absent`) |
| `vote_group_results` | vote × group | group_type (`party`, `government_opposition`, `district`), counts |
| `rollcall_sessions` | session | date, session_start, stated vs. parsed counts (quality check) |
| `rollcall_absences` | MP × session | mp_id, reason (`parliamentary_work`, `personal`, `other` = no reason given) |
| `rollcall_late_arrivals` | MP × session | mp_id, arrival_time, minutes_late |
| `speeches` | speech | mp_id, date, speech_type, matter, duration_min, words, text |
| `matters`, `matter_keywords` | matter | type, title, status, decision, first_signer_ids, other_signers |
| `mps` + `mp_terms`, `mp_party_history`, `mp_memberships`, `mp_interests`, `mp_minister_roles`, `mp_mandate_breaks`, `mp_substitutions`, `mp_education`, `mp_career` | MP / period | mp_id |
| `official_plenary_absence_totals`, `official_plenary_absence_daily`, `official_committee_absences` | MP (× day / committee-year) | mp_id matched by name, personal, other_no_reason |
| `ref_*` | reference | – |

### analytics/
| Table | What |
|---|---|
| `mp_scorecard` | One row per MP: vote participation %, votes against own party / party-line %, roll-call absences by reason (count and % of sessions while in office), late arrivals and minutes, official plenary & committee absences, speeches / minutes / words, initiatives & questions as first signer (by type), days in office, ministerial periods |
| `rebel_votes` | Every ballot where an MP voted against their party group's majority – with the vote title, for drill-down |
| `party_cohesion` | Per party: mean share of members voting with the group majority, share of unanimous votes |
| `party_agreement` | Party × party: % of votes where their majorities took the same position |

## Methodology & fairness notes (please keep these visible on the platform)

- **Absence ≠ laziness.** Ministers, the Speaker and MPs on parliamentary duties (committee trips, Council of Europe, OSCE …) miss votes by design. Use `reason` from the roll calls: only `other` (no reason given) is comparable across MPs. The official statistics already exclude parliamentary-work absences.
- **Normalise by time in office.** Substitutes and MPs who left mid-term (e.g. to the European Parliament) have fewer sessions; use the `_pct` columns / `days_in_office_this_term`.
- **Session vs. vote level.** Official statistics count *session days*; `ballots` count *individual votes*. They answer different questions – show both.
- **Late arrivals.** An MP registering within 15 minutes of roll call is recorded present and does not appear as late; the list shows those who registered later, with time.
- **Party line.** Majority is computed per vote among group members who voted; ties give no majority and are excluded. Many votes are procedural – consider weighting final-reading and budget votes more, and publish the rule.
- **Name matching.** Official CSVs and roll calls have names only; they are matched to MP ids via calling/first name + surname. Unmatched names are logged – check them after each run.
- **First signer.** `first_signer_ids` is extracted from the matter record; verify a sample against eduskunta.fi before publishing per-MP initiative counts.
- **Attribution.** Eduskunta open data is CC BY 4.0 – credit "Lähde: Eduskunta" on the platform.

## API facts verified live (Sept 2026)

- `User-Agent` header is mandatory (403 without it).
- `POST /search*`: 450 requests / 3000 s / IP – the client throttles itself.
- `/search` paging window is 10,000; `maxResults` ≤ 1000. Larger sets go through `/search/dataset` (NDJSON on S3).
- `/search` text matching is *ranked*, not strictly filtered – the pipeline post-filters every search result.
- The legacy `avoindata.eduskunta.fi` API shuts down at the end of 2026; this pipeline uses only the new API.
- Datawrapper chart URLs change version on every update; the pipeline always resolves the current version.
- `GET /taysistunnot/istunnon-aanestykset/{session}` returns **at most 10 votes** per session (budget sessions have 400+). Not used.
- A date range on `istuntopvm` misses votes; `istuntovpvuosi` (parliamentary year) + post-filter returns all 2,783 votes of the term (8 cancelled).

## Worth adding next (outside Eduskunta)

- **Election funding disclosures** (Valtiontalouden tarkastusvirasto, vaalirahoitusvalvonta.fi) – who funded each MP's 2023 campaign.
- **Transparency / lobbying register** (avoimuusrekisteri.fi, open API, CC BY 4.0) – lobbying contacts by topic.
- **2023 election results** (Statistics Finland / vaalit.fi) – votes per MP for context.
- **2027 candidate lists** once confirmed – to link sitting MPs' records to their candidacy.
