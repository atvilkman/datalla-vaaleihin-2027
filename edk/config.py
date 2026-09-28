"""Central settings. Change TERM_* when a new electoral term starts."""
from datetime import date, timedelta

API_BASE = "https://api.eduskunta.fi/api/v1"
TERM_ID = "2023-2026"            # value of `vaalikausitunnus` in documents / matters
TERM_START = "2023-04-05"        # first day of the term (reference-data/vaalikaudet)
TERM_END = (date.today() + timedelta(days=1)).isoformat()  # exclusive upper bound

# Put a real contact address here: the API returns 403 without a User-Agent,
# and a contact makes it easy for Eduskunta to reach you instead of blocking you.
USER_AGENT = "DataBasedVoting2027/1.0 (contact: you@example.com)"

# POST /search* is limited to 450 requests per 3000 s per IP. Stay a bit under.
POST_LIMIT = (430, 3000)
GET_DELAY_S = 0.25               # politeness delay between GETs

# Official absence statistics (Datawrapper embeds on eduskunta.fi absence page)
DW_PLENARY_TOTALS = "f4RJD"      # per MP: personal / other / total
DW_PLENARY_DAILY = "cJbCO"       # per MP per session day with reason
DW_COMMITTEE_SEED = "Y4axt"      # committee tables; links to all years x committees

LANG = "fi"
