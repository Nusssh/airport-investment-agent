"""Metrics engine: every metric definition and calculation. Deterministic, no LLM.

    base metrics       raw values per airport (traffic, delays, forecast, unmet-demand estimate)
    percentiles        position of a value within the national peer group (0-100)
    composite indices  weighted averages of percentiles, built in layers
    airport_profile    everything about one airport, including ranked drivers of unmet demand
"""
from functools import lru_cache
from math import erf, exp, pi, sqrt

import pandas as pd

import config
import data_sources as ds

T100 = "BTS T-100 API (live)"
ONTIME = "BTS On-Time Performance (files)"
DOMESTIC_ONLY = "Large US airlines, domestic flights only."

# Catalog: every metric the tools can return, with the definition and limitation passed to the LLM.
METRICS = {
    "passengers": dict(unit="passengers", source=T100,
                       definition="Passengers departing the airport in the last 12 months.",
                       limitation="Departing passengers only (enplanements)."),
    "departures": dict(unit="flights", source=T100,
                       definition="Departing flights in the last 12 months.",
                       limitation="Includes cargo flights."),
    "load_factor": dict(unit="%", source=T100,
                        definition="Passengers / seats on departing flights.",
                        limitation="Airlines decide how many seats to offer; a high value can reflect "
                                   "an airline's business model, not only a shortage at the airport."),
    "growth": dict(unit="%", source=T100,
                   definition="Passengers in the last 12 months vs. the 12 months before.",
                   limitation="One-off events (a route opening or closing) can distort it."),
    "seat_growth": dict(unit="%", source=T100,
                        definition="Seats offered in the last 12 months vs. the 12 months before.",
                        limitation="Supply side: decided by airlines and limited by the airport."),
    "demand_vs_supply": dict(unit="percentage points", source=T100,
                             definition="growth minus seat_growth. Positive = passengers grew faster than "
                                        "seats (the market is tightening).",
                             limitation="A one-year comparison."),
    "intl_share": dict(unit="%", source=T100,
                       definition="International departures / all departures.",
                       limitation="Counts flights, not passengers."),
    "unmet_demand_passengers": dict(unit="passengers per year", source=f"{T100} + spill model",
                                    definition="Estimated passengers who wanted a seat but could not get one "
                                               "because flights were full (spill model on monthly load "
                                               "factors). Central value; low/high range returned with it.",
                                    limitation="A model estimate that depends on an assumed day-to-day demand "
                                               "variability (hence the range). Travelers who never booked "
                                               "cannot be observed."),
    "unmet_demand_share": dict(unit="% of passengers served", source=f"{T100} + spill model",
                               definition="unmet_demand_passengers / passengers.",
                               limitation="Same as unmet_demand_passengers."),
    "near_full_months": dict(unit="months (of 12)", source=T100,
                             definition=f"Months with load factor >= {config.FULL_MONTH_LOAD_FACTOR:.0f}%.",
                             limitation="Monthly averages; individual flights vary."),
    "faa_forecast": dict(unit="% per year", source="FAA Terminal Area Forecast (file)",
                         definition="FAA-forecast yearly growth in passengers over the next 5 years "
                                    "(FAA forecasts unconstrained demand).",
                         limitation="A forecast, not an observation; FAA forecasts tend to be smooth."),
    "nas_delay": dict(unit="minutes per arrival", source=ONTIME,
                      definition="Arrival delay minutes BTS attributes to the National Airspace System "
                                 "(airport/airspace congestion, non-extreme weather), per arrival.",
                      limitation=f"{DOMESTIC_ONLY} Measures runway/airspace congestion, not terminal crowding."),
    "taxi_out": dict(unit="minutes", source=ONTIME,
                     definition="Average minutes from gate push-back to take-off.",
                     limitation=f"{DOMESTIC_ONLY} Also depends on airport layout."),
    "pct_delayed": dict(unit="%", source=ONTIME,
                        definition="Departures leaving 15+ minutes late, for any reason.",
                        limitation=f"{DOMESTIC_ONLY} Includes airline-caused delays."),
    "cancel_rate": dict(unit="%", source=ONTIME,
                        definition="Cancelled departures / scheduled departures.",
                        limitation=DOMESTIC_ONLY),
    "long_haul_share": dict(unit="%", source=ONTIME,
                            definition="Departures of 2,500+ miles / all departures.",
                            limitation=f"{DOMESTIC_ONLY} International and cargo flights are NOT included. "
                                       "2,500 mi is the finest distance band BTS publishes."),
    "congestion_index": dict(unit="score 0-100", source="computed",
                             definition="Average percentile of nas_delay, taxi_out and pct_delayed "
                                        "vs. US airports with 500K+ passengers.",
                             limitation="Congestion is measured through its effects (delays), not capacity."),
    "unmet_demand_index": dict(unit="score 0-100", source="computed",
                               definition="50% load_factor percentile + 50% congestion_index "
                                          "(full planes AND a strained airport).",
                               limitation="A relative indicator for ranking; see unmet_demand_passengers "
                                          "for an absolute estimate."),
    "momentum_index": dict(unit="score 0-100", source="computed",
                           definition="2/3 growth percentile + 1/3 faa_forecast percentile.",
                           limitation="Past growth does not guarantee future growth."),
    "expansion_index": dict(unit="score 0-100", source="computed",
                            definition="40% unmet_demand_index + 35% momentum_index + "
                                       "25% passengers percentile (scale).",
                            limitation="A screening indicator, not a profitability estimate. "
                                       "Weights are a judgment call."),
}
COMPOSITES = [m for m, d in METRICS.items() if d["source"] == "computed"]
PERCENTILE_OF = ["load_factor", "nas_delay", "taxi_out", "pct_delayed", "growth", "faa_forecast",
                 "passengers", "cancel_rate", "demand_vs_supply", "unmet_demand_share"]


# ---------------------------------------------------------------------------------------------
# Spill model: expected demand that exceeds the seats offered
# ---------------------------------------------------------------------------------------------
def _phi(z):
    return exp(-z * z / 2) / sqrt(2 * pi)


def _Phi(z):
    return 0.5 * (1 + erf(z / sqrt(2)))


@lru_cache(maxsize=None)
def spill_per_seat(load_factor_pct: float, cv: float) -> float:
    """Daily demand per flight ~ Normal(mu, cv*mu), seats = 1. Solve mu so that the expected
    boarded share equals the observed load factor; return expected demand above the seats."""
    lf = load_factor_pct / 100

    def boarded_and_spilled(mu):
        sigma = cv * mu
        z = (1 - mu) / sigma
        spilled = sigma * _phi(z) + (mu - 1) * (1 - _Phi(z))      # E[max(demand - seats, 0)]
        return mu - spilled, spilled

    lo, hi = 0.01, 5.0
    for _ in range(60):                                            # boarded share increases with mu
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if boarded_and_spilled(mid)[0] < lf else (lo, mid)
    return boarded_and_spilled((lo + hi) / 2)[1]


def _unmet_demand(monthly: pd.DataFrame) -> pd.DataFrame:
    """Per airport: spilled passengers over 12 months for each variability assumption."""
    m = monthly.copy()
    m["lf"] = (m["passengers"] / m["seats"] * 100).clip(upper=99.9).round(1)   # rounding enables caching
    out = pd.DataFrame(index=m["airport"].unique())
    for name, cv in config.SPILL_CV.items():
        spilled = m.apply(lambda r: spill_per_seat(r["lf"], cv) * r["seats"], axis=1)
        out[f"unmet_{name}"] = spilled.groupby(m["airport"]).sum()
    out["near_full_months"] = (m["lf"] >= config.FULL_MONTH_LOAD_FACTOR).groupby(m["airport"]).sum()
    return out


def _round_estimate(x):
    """Estimates are shown with 2 significant digits: no false precision."""
    if pd.isna(x) or x == 0:
        return x
    digits = len(str(int(abs(x)))) - 2
    return int(round(x, -digits)) if digits > 0 else int(round(x))


# ---------------------------------------------------------------------------------------------
# The metrics table
# ---------------------------------------------------------------------------------------------
def _weighted(df: pd.DataFrame, weights: dict) -> tuple[pd.Series, pd.Series]:
    """Weighted average of the components available per airport.
    Returns (value, comma-separated names of missing components)."""
    parts = pd.DataFrame({n: df[n] if n.endswith("_index") else df[f"pctl_{n}"] for n in weights})
    w = pd.Series(weights)
    available = parts.notna()
    total_w = available.mul(w).sum(axis=1)
    value = parts.fillna(0).mul(w).sum(axis=1) / total_w.where(total_w > 0)
    missing = available.apply(lambda row: ", ".join(row.index[~row]), axis=1)
    return value.round(1), missing


def build_table(live: bool = True) -> tuple[pd.DataFrame, dict]:
    """One row per airport: base metrics, percentiles, composite indices, cause breakdowns."""
    traffic, monthly, traffic_status = ds.load_traffic(live=live)
    delays, delays_status = ds.load_delays()
    taf, taf_status = ds.load_taf()
    ref, ref_status = ds.load_reference()
    pct = lambda a, b: (a / b.where(b > 0) - 1) * 100

    t = traffic.set_index("airport")
    df = pd.DataFrame(index=t.index)
    df["passengers"] = t["passengers"]
    df["departures"] = t["departures"]
    df["load_factor"] = (t["passengers"] / t["seats"].where(t["seats"] > 0) * 100).round(1)
    df["growth"] = pct(t["passengers"], t["passengers_prev"]).round(1)
    df["seat_growth"] = pct(t["seats"], t["seats_prev"]).round(1)
    df["demand_vs_supply"] = (df["growth"] - df["seat_growth"]).round(1)
    df["intl_share"] = (t["intl_departures"] / t["departures"].where(t["departures"] > 0) * 100).round(1)
    df["avg_passengers_per_departure"] = (t["passengers"] / t["departures"].where(t["departures"] > 0)).round(1)

    u = _unmet_demand(monthly)
    for name in config.SPILL_CV:
        df[f"unmet_demand_{name}"] = u[f"unmet_{name}"].map(_round_estimate)
    df["unmet_demand_passengers"] = df["unmet_demand_central"]
    df["unmet_demand_share"] = (u["unmet_central"] / df["passengers"] * 100).round(1)
    df["near_full_months"] = u["near_full_months"]

    d = delays.set_index("airport")
    df["nas_delay"] = d["nas_delay_min_per_arrival"].round(2)
    df["taxi_out"] = d["avg_taxi_out_min"].round(1)
    df["pct_delayed"] = (d["pct_dep_delayed_15"] * 100).round(1)
    df["cancel_rate"] = (d["cancel_rate"] * 100).round(2)
    df["long_haul_share"] = (d["long_haul_share"] * 100).round(1)
    df = df.join(d[[c for c in d.columns if c.startswith("total_")]])

    # BTS uses IATA codes; the FAA forecast is joined on the FAA code (they differ for ~100 airports)
    ref_cols = ["name", "city", "state", "state_name", "census_region", "census_division", "faa_code"]
    df = df.join(ref.set_index("airport")[ref_cols], how="inner")
    df = df.join(taf.set_index("airport")["taf_forecast_cagr"].mul(100).round(1).rename("faa_forecast"),
                 on="faa_code")

    peers = df["in_peer_group"] = df["passengers"] >= config.PEER_MIN_PASSENGERS
    for m in PERCENTILE_OF:
        df[f"pctl_{m}"] = (df.loc[peers, m].rank(pct=True) * 100).round(1)

    df["congestion_index"], df["congestion_index_missing"] = _weighted(df, config.CONGESTION_WEIGHTS)
    df["unmet_demand_index"], df["unmet_demand_index_missing"] = _weighted(df, config.UNMET_DEMAND_WEIGHTS)
    df["momentum_index"], df["momentum_index_missing"] = _weighted(df, config.MOMENTUM_WEIGHTS)
    df["expansion_index"], df["expansion_index_missing"] = _weighted(df, config.EXPANSION_WEIGHTS)
    # propagate missing inputs to every index built on top of them
    merge = lambda *cols: df[list(cols)].apply(
        lambda r: ", ".join(sorted({x for c in r for x in c.split(", ") if x})), axis=1)
    df["unmet_demand_index_missing"] = merge("unmet_demand_index_missing", "congestion_index_missing")
    df["expansion_index_missing"] = merge("expansion_index_missing", "unmet_demand_index_missing",
                                          "momentum_index_missing")
    for idx in COMPOSITES:                                         # scores only for the peer group
        df.loc[~peers, [idx, f"{idx}_missing"]] = [float("nan"), ""]

    sources = {"traffic": traffic_status, "delays": delays_status, "faa_forecast": taf_status,
               "airports": ref_status, "peer_group_size": int(peers.sum())}
    return df.reset_index().rename(columns={"index": "airport"}), sources


_cache = {"table": None, "sources": None}


def get_table(live: bool = True) -> tuple[pd.DataFrame, dict]:
    """Built once per app start (live API) and on "Refresh data"; BTS publishes monthly, so
    re-querying during a session only adds latency (measured 2-20s per pull)."""
    if _cache["table"] is None:
        _cache["table"], _cache["sources"] = build_table(live=live)
    return _cache["table"], _cache["sources"]


def refresh_table() -> dict:
    """Re-query the live API and rebuild the table (the app's refresh button)."""
    _cache["table"] = None
    return get_table()[1]


# ---------------------------------------------------------------------------------------------
# Queries used by the tools
# ---------------------------------------------------------------------------------------------
def rank(df: pd.DataFrame, metric: str, top: int = 10, ascending: bool = False) -> pd.DataFrame:
    """Airports sorted by a metric; airports without a value are excluded."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric '{metric}'")
    return df[df[metric].notna()].sort_values(metric, ascending=ascending).head(top)


def rank_label(df: pd.DataFrame, metric: str, airport: str) -> str | None:
    """'3 of 141 in the US; 1 of 7 in New England (highest first)' - positions are computed here,
    never inferred by the LLM."""
    row = df[df["airport"] == airport]
    if row.empty or pd.isna(row[metric].iloc[0]):
        return None
    division = row["census_division"].iloc[0]
    us = df[df[metric].notna()]
    dv = us[us["census_division"] == division]
    pos = lambda sub: int((sub[metric] > row[metric].iloc[0]).sum()) + 1
    return f"{pos(us)} of {len(us)} in the US; {pos(dv)} of {len(dv)} in {division} (highest first)"


def airport_profile(airport: str) -> dict:
    """Everything about one airport: metrics with US percentiles, scores with ranks, the
    unmet-demand estimate and its drivers, delay and cancellation causes, assumptions."""
    df, sources = get_table()
    code = airport.upper()
    row = df[df["airport"] == code]
    if row.empty:
        return {"error": f"No data for airport '{airport}'."}
    r = row.iloc[0]

    with_pctl = lambda m, value: {"value": value, "percentile_vs_us": r.get(f"pctl_{m}")}
    share = lambda col, total: (round(r[col] / total * 100, 1)
                                if pd.notna(r.get(col)) and total and pd.notna(total) else None)
    cancelled = r["total_cancelled"]
    delay_cols = {"airline": "total_dep_carrier_delay", "weather": "total_dep_weather_delay",
                  "airspace/airport congestion (NAS)": "total_dep_nas_delay",
                  "security": "total_dep_security_delay", "late incoming aircraft": "total_dep_late_aircraft_delay"}
    delay_total = sum(r[c] for c in delay_cols.values() if pd.notna(r[c]))

    # Drivers of unmet demand: shown only if the data points that way and the airport is above
    # the national median for that signal; strongest (highest percentile) first.
    drivers = [
        ("Planes are nearly full", f"{r['load_factor']}% average load factor", "load_factor", True),
        ("Airport/airspace congestion makes it hard to add flights",
         f"{r['nas_delay']} min congestion delay per arrival", "nas_delay", True),
        ("Passenger demand grew faster than seats",
         f"passengers {r['growth']:+}% vs seats {r['seat_growth']:+}%", "demand_vs_supply",
         pd.notna(r["demand_vs_supply"]) and r["demand_vs_supply"] > 0),
        ("Many flights cancelled", f"{r['cancel_rate']}% of departures", "cancel_rate", True),
    ]
    drivers = [{"driver": d, "evidence": e, "percentile_vs_us": r[f"pctl_{m}"]}
               for d, e, m, applies in drivers
               if applies and pd.notna(r[f"pctl_{m}"]) and r[f"pctl_{m}"] >= 50]
    drivers.sort(key=lambda x: -x["percentile_vs_us"])

    return {
        "airport": code, "name": r["name"], "city": r["city"], "state": r["state"],
        "census_division": r["census_division"],
        "in_peer_group": bool(r["in_peer_group"]),
        "traffic": {m: with_pctl(m, r[m]) for m in
                    ["passengers", "departures", "load_factor", "growth", "seat_growth", "intl_share"]},
        "congestion": {m: with_pctl(m, r[m]) for m in ["nas_delay", "taxi_out", "pct_delayed", "cancel_rate"]},
        "outlook": {"faa_forecast": with_pctl("faa_forecast", r["faa_forecast"])},
        "long_haul_share": r["long_haul_share"],
        "scores": {idx: {"score": r[idx], "rank": rank_label(df, idx, code),
                         "partial_missing_inputs": r[f"{idx}_missing"] or None} for idx in COMPOSITES},
        "unmet_demand": {
            "definition": METRICS["unmet_demand_passengers"]["definition"],
            "passengers_per_year": {k: r[f"unmet_demand_{k}"] for k in config.SPILL_CV},
            "share_of_passengers_served_pct": r["unmet_demand_share"],
            "near_full_months": r["near_full_months"],
            "rank": rank_label(df, "unmet_demand_passengers", code),
            "drivers_ranked": drivers,
        },
        "cancellations": {
            "flights": r["total_cancelled"],
            "share_by_cause_pct": {k: share(c, cancelled) for k, c in
                                   {"airline": "total_cancel_carrier", "weather": "total_cancel_weather",
                                    "airspace/airport congestion (NAS)": "total_cancel_nas",
                                    "security": "total_cancel_security"}.items()},
            "note": "Cancellations are disruption, not unmet demand: most passengers re-book.",
        },
        "delay_minutes_share_by_cause_pct": {k: share(c, delay_total) for k, c in delay_cols.items()},
        "assumptions": [
            f"Unmet demand: spill model with day-to-day demand variability cv {config.SPILL_CV['low']}-"
            f"{config.SPILL_CV['high']} (central {config.SPILL_CV['central']}) -> reported as a range.",
            "Monthly averages; individual flights differ.",
            "Travelers who never searched or booked cannot be observed (booking data is not public).",
            f"Delay, cancellation and long-haul data: {DOMESTIC_ONLY}",
        ],
        "data_periods": {"traffic": sources["traffic"].get("period"), "delays": sources["delays"].get("period")},
    }


if __name__ == "__main__":
    # The four assignment questions answered by code only (no LLM).
    import json
    df, sources = get_table()
    print("Traffic:", sources["traffic"]["mode"], sources["traffic"].get("period"),
          f"| peer group: {sources['peer_group_size']}")
    show = lambda x, cols: print(x[["airport"] + cols].to_string(index=False), "\n")
    ne = df[df["census_division"] == "New England"]
    print("New England - expansion_index")
    show(rank(ne, "expansion_index"), ["expansion_index", "unmet_demand_index", "momentum_index",
                                       "pctl_passengers", "expansion_index_missing"])
    print("LAX vs SNA - congestion")
    show(df[df["airport"].isin(["LAX", "SNA"])], ["congestion_index", "nas_delay", "taxi_out", "pct_delayed"])
    print("ANC - long-haul share")
    show(df[df["airport"] == "ANC"], ["long_haul_share", "intl_share"])
    print("SFO - unmet demand")
    print(json.dumps(airport_profile("SFO")["unmet_demand"], indent=2, default=str))
