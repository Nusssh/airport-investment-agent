"""Every tunable setting and stated assumption, in one place."""
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

# ---------- Data sources (public, no key required) ----------
T100_API_URL = "https://data.bts.gov/resource/r495-tyji.json"     # BTS T-100 by origin airport (Socrata)
T100_MIN_PASSENGERS = 50_000            # over 24 months; excludes tiny airstrips
TRAFFIC_API_TIMEOUT = 10                # s; measured 2-20s - beyond this the cached copy is used

ONTIME_URL = ("https://transtats.bts.gov/PREZIP/"
              "On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{year}_{month}.zip")
ONTIME_MONTHS = 12

TAF_PAGE = "https://taf.faa.gov/"       # newest FAA TAF edition is discovered on this page
TAF_URL_TEMPLATE = "https://taf.faa.gov/Downloads/APO100_TAF_Final_{edition}.zip"
TAF_FALLBACK_EDITION = 2025
TAF_FORECAST_YEARS = 5

OURAIRPORTS_AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OURAIRPORTS_REGIONS_URL = "https://davidmegginson.github.io/ourairports-data/regions.csv"
CENSUS_GEOCODES_URL = ("https://www2.census.gov/programs-surveys/popest/geographies/"
                       "{year}/state-geocodes-v{year}.xlsx")
REFERENCE_MAX_AGE_DAYS = 30

# ---------- Sanity checks: suspicious downloads never replace good data ----------
MIN_AIRPORTS_T100 = 150
MIN_AIRPORTS_ONTIME = 250
MIN_FLIGHTS_ONTIME = 300_000
MIN_AIRPORTS_TAF = 400

# ---------- Metric definitions (assumptions) ----------
LONG_HAUL_DISTANCE_GROUP = 11           # BTS distance group 11 = 2,500+ miles (finest band available)
PEER_MIN_PASSENGERS = 500_000           # scores are percentiles vs. US airports at least this size
CONGESTION_WEIGHTS = {"nas_delay": 1 / 3, "taxi_out": 1 / 3, "pct_delayed": 1 / 3}
UNMET_DEMAND_WEIGHTS = {"load_factor": 0.5, "congestion_index": 0.5}
MOMENTUM_WEIGHTS = {"growth": 2 / 3, "faa_forecast": 1 / 3}          # observed growth weighted above forecast
EXPANSION_WEIGHTS = {"unmet_demand_index": 0.40, "momentum_index": 0.35, "passengers": 0.25}

# Spill model: day-to-day demand per flight varies with this coefficient of variation (std/mean).
# It cannot be observed, so the estimate is reported as a range.
SPILL_CV = {"low": 0.2, "central": 0.3, "high": 0.4}
FULL_MONTH_LOAD_FACTOR = 85.0           # % - months at or above count as "near full"

# ---------- Language models ----------
# One model, plus a fallback from a second provider that is used only if its key is configured:
# on 06/10 Gemini returned 503/504/429 several times, once for all its models at the same moment.
LLM_CHAIN = [
    ("gemini", "models/gemini-3.5-flash-lite"),
    ("groq", "openai/gpt-oss-120b"),
]
LLM_TIMEOUT_SECONDS = 20
MODEL_COOLDOWN_SECONDS = 60             # a model that just failed is tried last for this long
GROQ_MAX_WAIT_SECONDS = 12              # honor Groq's "try again in Xs" once if X is this short
HISTORY_MESSAGES = 6                    # last 3 exchanges sent to the model (follow-ups vs. tokens)
