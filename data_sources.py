"""Data layer: fetches public aviation data, keeps it fresh, never silently serves bad data.

Sources
  1. BTS T-100 (REST API)   live calls; server-side aggregation; cached copy as fallback
  2. BTS On-Time (files)    one summary per month; refresh downloads only missing months
  3. FAA TAF (file)         newest edition discovered automatically
  4. Airport reference      OurAirports + US Census regions, cross-checked against FAA

Every refresh passes sanity checks before replacing existing data, and every dataset records
its source and timestamp in data/metadata.json (shown in the app).
Run `python data_sources.py` to refresh from the terminal; the app's refresh button calls refresh_all().
"""
import io
import json
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import pandas as pd
import requests

import config

TRAFFIC_CACHE = config.DATA_DIR / "traffic_cache.csv"
MONTHLY_CACHE = config.DATA_DIR / "traffic_monthly_cache.csv"
ONTIME_DIR = config.DATA_DIR / "ontime"           # one summary file per month
TAF_FILE = config.DATA_DIR / "taf_summary.csv"
AIRPORTS_FILE = config.DATA_DIR / "airports_reference.csv"
META_FILE = config.DATA_DIR / "metadata.json"


class SanityCheckFailed(Exception):
    """Downloaded data looks wrong; the previous good data is kept."""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------- metadata: where each dataset came from and when ----------
def load_meta() -> dict:
    if META_FILE.exists():
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    return {}


def _save_meta(key: str, info: dict) -> None:
    meta = load_meta()
    meta[key] = info
    META_FILE.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


# =====================================================================
# 1. T-100 traffic: live API, server-side aggregation, cached fallback
# =====================================================================
def load_traffic(live: bool = True) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Returns (per-airport 12-month totals, per-airport monthly rows, status).
    Tries the live API; on any failure returns the last good copy with mode="cache" and the error."""
    error = None
    if live:
        try:
            totals, monthly, status = _fetch_traffic_live()
            if totals["airport"].nunique() < config.MIN_AIRPORTS_T100:
                raise SanityCheckFailed(f"only {totals['airport'].nunique()} airports returned")
            config.DATA_DIR.mkdir(exist_ok=True)
            totals.to_csv(TRAFFIC_CACHE, index=False)
            monthly.to_csv(MONTHLY_CACHE, index=False)
            _save_meta("traffic", status)
            return totals, monthly, status
        except Exception as e:  # network, HTTP error, timeout, failed sanity check
            error = f"{type(e).__name__}: {e}"

    if not (TRAFFIC_CACHE.exists() and MONTHLY_CACHE.exists()):
        raise RuntimeError(f"T-100 API failed and no cached copy exists ({error})")
    status = {**load_meta().get("traffic", {}), "mode": "cache", "live_error": error}
    return pd.read_csv(TRAFFIC_CACHE), pd.read_csv(MONTHLY_CACHE), status


def _get(params: dict) -> list[dict]:
    resp = requests.get(config.T100_API_URL, params=params, timeout=config.TRAFFIC_API_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _fetch_traffic_live() -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    started = datetime.now()
    # 1) newest published month -> the two 12-month windows
    newest = pd.Timestamp(_get({"$select": "max(reporting_month) as m"})[0]["m"])
    split = newest - pd.DateOffset(months=11)           # first month of the last 12
    oldest = newest - pd.DateOffset(months=23)          # first month of the previous 12
    s, o, n = (d.strftime("%Y-%m-%d") for d in (split, oldest, newest))

    # 2) per-airport sums for both windows, computed by the BTS server ($group + case)
    recent = lambda col: f"sum(case(reporting_month >= '{s}', {col}, true, 0))"
    previous = lambda col: f"sum(case(reporting_month < '{s}', {col}, true, 0))"
    totals = pd.DataFrame(_get({
        "$select": (f"origin_airport_code as airport, {recent('total_passengers')} as passengers, "
                    f"{previous('total_passengers')} as passengers_prev, {recent('total_seats')} as seats, "
                    f"{previous('total_seats')} as seats_prev, {recent('total_departures')} as departures, "
                    f"{recent('outbound_international')} as intl_departures"),
        "$where": f"reporting_month between '{o}' and '{n}'",
        "$group": "origin_airport_code",
        "$having": f"sum(total_passengers) > {config.T100_MIN_PASSENGERS}",
        "$limit": 5000,
    }))
    # 3) monthly passengers/seats per airport for the last 12 months (unmet-demand model)
    monthly = pd.DataFrame(_get({
        "$select": "origin_airport_code as airport, reporting_month as month, "
                   "total_passengers as passengers, total_seats as seats",
        "$where": f"reporting_month between '{s}' and '{n}' AND total_seats > 0 AND total_passengers >= 1000",
        "$limit": 20000,
    }))
    for df in (totals, monthly):
        for col in df.columns.drop(["airport", "month"], errors="ignore"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    monthly["month"] = pd.to_datetime(monthly["month"]).dt.strftime("%Y-%m")

    status = {
        "mode": "live", "fetched_at": _now(), "api_calls": 3,
        "seconds": round((datetime.now() - started).total_seconds(), 1),
        "period": f"{split:%Y-%m} to {newest:%Y-%m}",
        "previous_period": f"{oldest:%Y-%m} to {split - pd.DateOffset(months=1):%Y-%m}",
        "airports": len(totals), "source": config.T100_API_URL,
    }
    return totals, monthly, status


# =====================================================================
# 2. On-Time: one summary file per month, incremental refresh
# =====================================================================
ONTIME_SCHEMA = "v2"   # v2 adds cancellation causes + delay minutes by cause; older files are ignored


def _month_file(y: int, m: int):
    return ONTIME_DIR / f"{y}_{m:02d}_{ONTIME_SCHEMA}.csv"


def _prev(y: int, m: int) -> tuple[int, int]:
    return (y, m - 1) if m > 1 else (y - 1, 12)


def latest_ontime_window() -> list[tuple[int, int]]:
    """The newest N months that BTS has published (asks the server, newest first)."""
    y, m = date.today().year, date.today().month
    for _ in range(12):
        if requests.head(config.ONTIME_URL.format(year=y, month=m), timeout=20).status_code == 200:
            break
        y, m = _prev(y, m)
    else:
        raise RuntimeError("No on-time file found in the last 12 months - did the BTS URL change?")
    window = []
    for _ in range(config.ONTIME_MONTHS):
        window.append((y, m))
        y, m = _prev(y, m)
    return window


def missing_ontime_months() -> list[tuple[int, int]]:
    """Months that are published but not downloaded yet. Empty list = up to date."""
    return [ym for ym in latest_ontime_window() if not _month_file(*ym).exists()]


def _summarize_ontime_month(y: int, m: int) -> pd.DataFrame:
    """Download one month and reduce ~600k flights to one row per airport (sums + counts).
    Sums/counts (not averages) so that months can be combined correctly later."""
    resp = requests.get(config.ONTIME_URL.format(year=y, month=m), timeout=300)
    resp.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    csv_name = next(n for n in z.namelist() if n.endswith(".csv"))
    cols = ["Origin", "Dest", "Cancelled", "CancellationCode", "DepDel15", "TaxiOut", "TaxiIn", "DistanceGroup",
            "CarrierDelay", "WeatherDelay", "NASDelay", "SecurityDelay", "LateAircraftDelay"]
    f = pd.read_csv(z.open(csv_name), usecols=cols, low_memory=False)
    # BTS cancellation codes: A = airline, B = weather, C = National Airspace System, D = security
    for code, name in {"A": "carrier", "B": "weather", "C": "nas", "D": "security"}.items():
        f[f"cancel_{name}"] = (f["CancellationCode"] == code).astype(int)

    n_airports = f["Origin"].nunique()
    if len(f) < config.MIN_FLIGHTS_ONTIME or n_airports < config.MIN_AIRPORTS_ONTIME:
        raise SanityCheckFailed(f"{y}-{m:02d}: {len(f)} flights, {n_airports} airports")

    f["long_haul"] = f["DistanceGroup"] >= config.LONG_HAUL_DISTANCE_GROUP
    dep = f.groupby("Origin").agg(           # departures side: airport = Origin
        dep_flights=("Origin", "size"),
        cancelled=("Cancelled", "sum"),
        dep_delayed_15=("DepDel15", "sum"),
        dep_delay_known=("DepDel15", "count"),
        taxi_out_sum=("TaxiOut", "sum"),
        taxi_out_count=("TaxiOut", "count"),
        long_haul_flights=("long_haul", "sum"),
        cancel_carrier=("cancel_carrier", "sum"),
        cancel_weather=("cancel_weather", "sum"),
        cancel_nas=("cancel_nas", "sum"),
        cancel_security=("cancel_security", "sum"),
        # delay minutes by cause, for flights departing this airport
        dep_carrier_delay=("CarrierDelay", "sum"),
        dep_weather_delay=("WeatherDelay", "sum"),
        dep_nas_delay=("NASDelay", "sum"),
        dep_security_delay=("SecurityDelay", "sum"),
        dep_late_aircraft_delay=("LateAircraftDelay", "sum"),
    )
    arr = f.groupby("Dest").agg(             # arrivals side: airport = Dest
        arr_flights=("Dest", "size"),
        taxi_in_sum=("TaxiIn", "sum"),
        taxi_in_count=("TaxiIn", "count"),
        nas_delay_sum=("NASDelay", "sum"),   # minutes blamed on airspace/airport congestion
    )
    out = dep.join(arr, how="outer").fillna(0)
    out.index.name = "airport"
    return out.reset_index()


def refresh_ontime(progress=print) -> list[str]:
    """Downloads only the missing months. Returns a list of warnings (empty = all good).
    Measured: one month = ~58s download + ~7s processing -> the network is the bottleneck,
    so several months are downloaded in parallel."""
    ONTIME_DIR.mkdir(parents=True, exist_ok=True)
    warnings = []
    todo = missing_ontime_months()
    progress(f"on-time: {len(todo)} month(s) to download")
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(_summarize_ontime_month, y, m): (y, m) for y, m in todo}
        for done, job in enumerate(as_completed(jobs), 1):
            y, m = jobs[job]
            try:
                job.result().to_csv(_month_file(y, m), index=False)
                progress(f"on-time {y}-{m:02d} saved ({done}/{len(todo)})")
            except Exception as e:
                warnings.append(f"on-time {y}-{m:02d} skipped: {type(e).__name__}: {e}")
    return warnings


def load_delays() -> tuple[pd.DataFrame, dict]:
    """Combine the newest N monthly summaries into one row per airport with averages/rates."""
    files = sorted(ONTIME_DIR.glob(f"*_{ONTIME_SCHEMA}.csv"))[-config.ONTIME_MONTHS:]
    if not files:
        raise RuntimeError("No on-time data yet - run data_sources.py or press 'Refresh data'")
    s = pd.concat([pd.read_csv(p) for p in files]).groupby("airport").sum()

    out = pd.DataFrame(index=s.index)
    out["ot_departures"] = s["dep_flights"]
    out["cancel_rate"] = s["cancelled"] / s["dep_flights"]
    out["pct_dep_delayed_15"] = s["dep_delayed_15"] / s["dep_delay_known"]
    out["avg_taxi_out_min"] = s["taxi_out_sum"] / s["taxi_out_count"]
    out["long_haul_share"] = s["long_haul_flights"] / s["dep_flights"]
    out["avg_taxi_in_min"] = s["taxi_in_sum"] / s["taxi_in_count"]
    out["nas_delay_min_per_arrival"] = s["nas_delay_sum"] / s["arr_flights"]
    # 12-month totals for "why" questions about a single airport
    for c in ["cancelled", "cancel_carrier", "cancel_weather", "cancel_nas", "cancel_security",
              "dep_carrier_delay", "dep_weather_delay", "dep_nas_delay", "dep_security_delay",
              "dep_late_aircraft_delay"]:
        out[f"total_{c}"] = s[c]

    period = f"{files[0].stem[:7].replace('_', '-')} to {files[-1].stem[:7].replace('_', '-')}"
    status = {"mode": "file", "period": period, "months": len(files),
              "coverage_note": "Large US airlines, domestic flights only"}
    if len(files) < config.ONTIME_MONTHS:
        status["warning"] = f"only {len(files)} of {config.ONTIME_MONTHS} months available"
    return out.reset_index(), status


# =====================================================================
# 3. TAF: auto-discover newest edition
# =====================================================================
def latest_taf_edition() -> tuple[int, str | None]:
    """Find the newest TAF edition linked on the FAA site. Falls back to the configured edition."""
    try:
        html = requests.get(config.TAF_PAGE, timeout=30).text
        editions = [int(y) for y in re.findall(r"APO100_TAF_Final_(\d{4})\.zip", html)]
        if editions:
            return max(editions), None
        return config.TAF_FALLBACK_EDITION, "no TAF download link found on FAA page"
    except Exception as e:
        return config.TAF_FALLBACK_EDITION, f"FAA page unreachable: {e}"


def taf_update_available() -> bool:
    current = load_meta().get("taf", {}).get("edition")
    newest, _ = latest_taf_edition()
    return current is None or newest > current


def refresh_taf(progress=print) -> list[str]:
    warnings = []
    edition, problem = latest_taf_edition()
    if problem:
        warnings.append(f"TAF: {problem}; using edition {edition}")
    progress(f"FAA TAF edition {edition}")
    try:
        resp = requests.get(config.TAF_URL_TEMPLATE.format(edition=edition), timeout=300)
        resp.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        airports = pd.read_excel(z.open("Airports.xlsx"), usecols=["LOCID", "APORT_NAME", "CITY", "STATE", "HUB_SIZE"])
        enpl = pd.read_excel(z.open("Enplanements.xlsx"))
    except Exception as e:
        return warnings + [f"TAF download failed, keeping previous data: {type(e).__name__}: {e}"]

    for df in (airports, enpl):
        df.columns = [c.lower() for c in df.columns]
        df["locid"] = df["locid"].astype(str).str.strip()

    # ASSUMPTION: total enplanements = sum of all carrier-type columns.
    # Sanity check done by hand: BOS 2024 = ~20.9M, close to Logan's published ~21M enplanements.
    enpl["enplanements"] = enpl[["aac", "aat", "commuter", "us_flag", "frgn_flag"]].sum(axis=1)
    wide = enpl.pivot_table(index="locid", columns="ayear", values="enplanements", aggfunc="sum")
    y0, y1 = edition, edition + config.TAF_FORECAST_YEARS
    if y0 not in wide.columns or y1 not in wide.columns:
        return warnings + [f"TAF {edition}: years {y0}/{y1} missing, keeping previous data"]

    out = pd.DataFrame({"taf_enpl_start": wide[y0], "taf_enpl_end": wide[y1]})
    out = out[out["taf_enpl_start"] > 0]
    out["taf_forecast_cagr"] = (out["taf_enpl_end"] / out["taf_enpl_start"]) ** (1 / (y1 - y0)) - 1
    out = airports.set_index("locid").join(out, how="inner").reset_index()
    out = out.rename(columns={"locid": "airport", "aport_name": "faa_name"})

    if len(out) < config.MIN_AIRPORTS_TAF or "BOS" not in set(out["airport"]):
        return warnings + [f"TAF {edition}: sanity check failed ({len(out)} airports), keeping previous data"]

    config.DATA_DIR.mkdir(exist_ok=True)
    out.to_csv(TAF_FILE, index=False)
    _save_meta("taf", {"mode": "file", "edition": edition, "built_at": _now(),
                       "forecast_years": f"{y0}->{y1}", "source": config.TAF_URL_TEMPLATE.format(edition=edition)})
    return warnings


def load_taf() -> tuple[pd.DataFrame, dict]:
    if not TAF_FILE.exists():
        raise RuntimeError("No TAF data yet - run data_sources.py or press 'Refresh data'")
    return pd.read_csv(TAF_FILE), load_meta().get("taf", {})


# =====================================================================
# 4. Airport reference: OurAirports + official Census regions, cross-checked with FAA
# =====================================================================
def _census_divisions() -> pd.DataFrame:
    """State name -> Census region + division, from the newest Census geocodes file."""
    for year in range(date.today().year, date.today().year - 5, -1):
        resp = requests.get(config.CENSUS_GEOCODES_URL.format(year=year), timeout=60)
        if resp.status_code == 200:
            break
    else:
        raise RuntimeError("Census geocodes file not found for the last 5 years")
    raw = pd.read_excel(io.BytesIO(resp.content), header=None, dtype=str)
    header_row = raw.index[raw[0].str.strip().eq("Region")][0]
    g = raw.iloc[header_row + 1:, :4].dropna()
    g.columns = ["region", "division", "fips", "name"]
    region_names = g[(g.division == "0") & (g.fips == "00")].set_index("region")["name"]
    division_names = g[(g.division != "0") & (g.fips == "00")].set_index("division")["name"]
    states = g[g.fips != "00"].copy()
    states["census_region"] = states.region.map(region_names).str.replace(" Region", "")
    states["census_division"] = states.division.map(division_names).str.replace(" Division", "")
    states.attrs["year"] = year
    return states.rename(columns={"name": "state_name"})[["state_name", "census_region", "census_division"]]


def reference_is_stale() -> bool:
    built = load_meta().get("airports", {}).get("built_at")
    if not built or not AIRPORTS_FILE.exists():
        return True
    return (datetime.now() - datetime.strptime(built, "%Y-%m-%d %H:%M")).days >= config.REFERENCE_MAX_AGE_DAYS


def refresh_reference(progress=print) -> list[str]:
    progress("airport reference (OurAirports + Census)")
    try:
        ap = pd.read_csv(config.OURAIRPORTS_AIRPORTS_URL, keep_default_na=False, na_values=[""])
        regions = pd.read_csv(config.OURAIRPORTS_REGIONS_URL, keep_default_na=False, na_values=[""])
        census = _census_divisions()
    except Exception as e:
        return [f"Airport reference download failed, keeping previous data: {type(e).__name__}: {e}"]

    # US airports that have an IATA code (the code BTS uses, e.g. BOS) and scheduled service
    ap = ap[(ap.iso_country == "US") & ap.iata_code.notna() & (ap.scheduled_service == "yes")]
    ap = ap.merge(regions[["code", "name"]].rename(columns={"code": "iso_region", "name": "state_name"}),
                  on="iso_region", how="left")
    ap = ap.merge(census, on="state_name", how="left")
    ref = pd.DataFrame({
        # BTS uses IATA codes; FAA uses its own location IDs. Usually identical, but not always
        # (e.g. IATA "CHU" = Chuathbaluk, AK while FAA "CHU" = Houston County, MN) -> keep both.
        "airport": ap.iata_code, "faa_code": ap.local_code, "name": ap["name"], "city": ap.municipality,
        "state": ap.iso_region.str.replace("US-", ""), "state_name": ap.state_name,
        "census_region": ap.census_region, "census_division": ap.census_division,
        "lat": ap.latitude_deg, "lon": ap.longitude_deg, "size": ap.type,
        "keywords": ap.keywords.fillna(""),
    }).drop_duplicates("airport")

    if len(ref) < config.MIN_AIRPORTS_T100 or "BOS" not in set(ref.airport):
        return [f"Airport reference sanity check failed ({len(ref)} airports), keeping previous data"]

    warnings = []
    # Cross-check: does the community data agree with the official FAA state for each airport?
    mismatches = []
    if TAF_FILE.exists():
        faa = pd.read_csv(TAF_FILE)[["airport", "state"]].rename(columns={"airport": "faa_code", "state": "faa_state"})
        both = ref.merge(faa, on="faa_code")
        mismatches = both[both.state != both.faa_state]
        if len(mismatches):
            warnings.append(f"State differs between OurAirports and FAA for {len(mismatches)} airports: "
                            f"{', '.join(mismatches.airport.head(10))}")

    config.DATA_DIR.mkdir(exist_ok=True)
    ref.to_csv(AIRPORTS_FILE, index=False)
    _save_meta("airports", {
        "mode": "file", "built_at": _now(), "airports": len(ref),
        "census_geocodes_year": census.attrs["year"],
        "faa_cross_check": (f"{len(both)} airports compared, {len(mismatches)} state mismatches"
                            if TAF_FILE.exists() else "not run (no FAA data yet)"),
        "source": config.OURAIRPORTS_AIRPORTS_URL,
    })
    return warnings


def load_reference() -> tuple[pd.DataFrame, dict]:
    if not AIRPORTS_FILE.exists():
        raise RuntimeError("No airport reference yet - run data_sources.py or press 'Refresh data'")
    return pd.read_csv(AIRPORTS_FILE, keep_default_na=False, na_values=[""]), load_meta().get("airports", {})


# =====================================================================
# Everything together
# =====================================================================
def refresh_all(progress=print) -> list[str]:
    """Bring every offline source up to date. Safe to run any time: only downloads what's new."""
    warnings = []
    if taf_update_available():
        warnings += refresh_taf(progress)
    else:
        progress("FAA TAF already up to date")
    if reference_is_stale():                 # after TAF, so the FAA cross-check can run
        warnings += refresh_reference(progress)
    else:
        progress("airport reference already up to date")
    warnings += refresh_ontime(progress)
    return warnings


if __name__ == "__main__":
    print("1/3 T-100 traffic (live API)")
    traffic, _, status = load_traffic(live=True)
    print(f"  {status['mode']}: {len(traffic)} airports, {status.get('period')} ({status.get('seconds')}s)",
          f"| live error: {status['live_error']}" if status.get("live_error") else "")

    print("2/3 + 3/3 TAF and on-time (first run: several minutes; later runs: only new data)")
    problems = refresh_all(progress=lambda msg: print("  ", msg, flush=True))

    taf, taf_status = load_taf()
    delays, delay_status = load_delays()
    print(f"  TAF edition {taf_status.get('edition')}: {len(taf)} airports")
    print(f"  Delays {delay_status['period']}: {len(delays)} airports")
    ref, ref_status = load_reference()
    print(f"  Airport reference: {len(ref)} airports | FAA cross-check: {ref_status.get('faa_cross_check')}")
    print("Warnings:" if problems else "No warnings.")
    for p in problems:
        print("  !", p)
    print("Done. Files in", config.DATA_DIR)
