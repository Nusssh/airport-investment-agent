"""Tools: the interface between the LLM and the deterministic code.

The LLM sees only these functions (name, docstring, parameters) and decides which to call;
every value in their results comes from metrics.py. Each call is recorded in TRACE so the UI
can show how an answer was computed.
"""
import functools
import time

import pandas as pd

import config
import data_sources as ds
import metrics

TRACE: list[dict] = []          # tool calls of the current question (cleared by the agent)


def _clean(value):
    """pandas/numpy values -> plain Python; NaN -> None."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and value != value:
        return None
    return value


def _traced(fn):
    @functools.wraps(fn)                      # keeps the signature and docstring visible to the LLM
    def wrapper(*args, **kwargs):
        started = time.time()
        result = _clean(fn(*args, **kwargs))
        TRACE.append({"tool": fn.__name__, "args": kwargs or list(args),
                      "seconds": round(time.time() - started, 2), "result": result})
        return result
    return wrapper


def _sources(sources: dict) -> str:
    """One compact line: data periods and whether the live API answered."""
    t = sources["traffic"]
    live = "live API" if t.get("mode") == "live" else "saved copy - live API unavailable"
    return (f"traffic {t.get('period')} (BTS T-100, {live}); delays {sources['delays'].get('period')} "
            f"(large US airlines, domestic); FAA TAF {sources['faa_forecast'].get('edition')}; "
            f"scores vs {sources['peer_group_size']} US airports with {config.PEER_MIN_PASSENGERS:,}+ passengers")


def _match_airports(query: str) -> pd.DataFrame:
    """Airports matching a code, city, name, state or Census region (largest first)."""
    ref, _ = ds.load_reference()
    df, _ = metrics.get_table()
    q = query.strip().lower()
    hit = ((ref["airport"].str.lower() == q) | (ref["state"].str.lower() == q)
           | (ref["state_name"].str.lower() == q) | (ref["census_division"].str.lower() == q)
           | (ref["census_region"].str.lower() == q))
    if len(q) >= 4:   # short strings would match inside words ('LA' in 'AtLAnta'): exact match only
        hit |= (ref["city"].fillna("").str.lower().str.contains(q, regex=False)
                | ref["name"].str.lower().str.contains(q, regex=False)
                | ref["keywords"].str.lower().str.contains(q, regex=False))
    found = ref[hit].merge(df[["airport", "passengers", "in_peer_group"]], on="airport", how="left")
    return found.sort_values("passengers", ascending=False, na_position="last")


def _resolve_scope(scope: str, df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """'US' | Census region/division | state code or name | comma-separated IATA codes."""
    low = scope.strip().lower()
    if low in ("", "us", "usa", "united states", "all"):
        return df, "all US airports"
    for col in ("census_division", "census_region"):
        hit = df[df[col].str.lower() == low]
        if not hit.empty:
            return hit, f"Census {col.split('_')[1]} '{scope}' (US Census Bureau definition)"
    hit = df[(df["state"].str.lower() == low) | (df["state_name"].str.lower() == low)]
    if not hit.empty:
        return hit, f"state {scope}"
    codes = [c.strip().upper() for c in scope.split(",") if c.strip()]
    return df[df["airport"].isin(codes)], f"airports {', '.join(codes)}"


# =============================================================================================
# Tools
# =============================================================================================
@_traced
def find_airports(queries: list[str]) -> dict:
    """Find US airports for one or more names in a single call, e.g. ["Los Angeles", "Santa Ana"].
    Each query can be an IATA code, city, airport name, state (code or name) or US Census
    region/division (e.g. "New England"). Use full city names ("Los Angeles", not "LA": LA is
    Louisiana's code). If a city matches several airports (e.g. Portland ME/OR), ask which one
    unless the context decides."""
    out = {}
    for q in queries:
        found = _match_airports(q)
        out[q] = found.head(10)[["airport", "name", "state", "passengers", "in_peer_group"]].to_dict("records")
    return {"matches": out,
            "note": "passengers = last 12 months; in_peer_group = has 0-100 scores (500K+ passengers)"}


@_traced
def get_metrics(airports: list[str], metric_names: list[str]) -> dict:
    """Values of chosen metrics for specific airports (IATA codes). metric_names: catalog names
    (see explain_metric('all')) or ['all']. Each value comes with its US percentile where
    available, its rank (in the US and in the airport's Census division), and for scores, any
    missing inputs. Definitions and limitations of the requested metrics are included."""
    df, sources = metrics.get_table()
    names = list(metrics.METRICS) if metric_names in (["all"], "all") else list(metric_names)
    unknown = [m for m in names if m not in metrics.METRICS]
    names = [m for m in names if m in metrics.METRICS]
    codes = [a.strip().upper() for a in airports]
    rows = df[df["airport"].isin(codes)]

    results = []
    for _, r in rows.iterrows():
        item = {"airport": r["airport"], "name": r["name"], "state": r["state"],
                "census_division": r["census_division"], "in_peer_group": r["in_peer_group"]}
        for m in names:
            item[m] = r[m]
            item[f"{m}_rank"] = metrics.rank_label(df, m, r["airport"])
            if f"pctl_{m}" in r:
                item[f"{m}_percentile_vs_us"] = r[f"pctl_{m}"]
            if r.get(f"{m}_missing"):
                item[f"{m}_partial_missing_inputs"] = r[f"{m}_missing"]
        if "unmet_demand_passengers" in names:
            item["unmet_demand_range"] = {"low": r["unmet_demand_low"], "high": r["unmet_demand_high"]}
        if "congestion_index" in names:
            item.update({c: r[c] for c in ("nas_delay", "taxi_out", "pct_delayed")})
        results.append(item)
    return {
        "airports": results,
        "not_found": [c for c in codes if c not in set(rows["airport"])],
        "unknown_metrics": unknown,
        "metrics": {m: {k: metrics.METRICS[m][k] for k in ("unit", "definition", "limitation")} for m in names},
        "data": _sources(sources),
    }


@_traced
def rank_airports(scope: str, metric: str, top: int = 10, lowest_first: bool = False,
                  min_passengers: int = 0) -> dict:
    """Rank airports by any catalog metric. scope: 'US', a US Census region/division (e.g.
    'New England'), a state (code or name), or comma-separated IATA codes. Scores (*_index)
    exist only for airports with 500K+ passengers. min_passengers filters by size (e.g. 1000000
    for 'large airports'; small airports can show extreme % changes)."""
    df, sources = metrics.get_table()
    if metric not in metrics.METRICS:
        return {"error": f"Unknown metric '{metric}'.", "known_metrics": list(metrics.METRICS)}
    sub, scope_desc = _resolve_scope(scope, df)
    if min_passengers:
        sub = sub[sub["passengers"] >= min_passengers]
        scope_desc += f", {min_passengers:,}+ passengers"
    if sub.empty:
        return {"error": f"No airports found for scope '{scope}'. Try find_airports first."}

    ranked = metrics.rank(sub, metric, top=top, ascending=lowest_first)
    components = {"expansion_index": ["unmet_demand_index", "momentum_index", "pctl_passengers"],
                  "unmet_demand_index": ["load_factor", "congestion_index"],
                  "congestion_index": ["nas_delay", "taxi_out", "pct_delayed"],
                  "unmet_demand_passengers": ["unmet_demand_low", "unmet_demand_high", "unmet_demand_share"],
                  }.get(metric, [])
    cols = ["airport", "name", "state", metric] + components + (["passengers"] if metric != "passengers" else [])
    if f"{metric}_missing" in ranked:
        cols.append(f"{metric}_missing")
    table = ranked[cols].rename(columns={"pctl_passengers": "scale_percentile",
                                         f"{metric}_missing": "partial_missing_inputs"})
    table.insert(0, "position", range(1, len(table) + 1))
    unranked = sub[sub[metric].isna()]
    return {
        "scope": scope_desc, "metric": metric, "order": "lowest first" if lowest_first else "highest first",
        "ranked_count": int(sub[metric].notna().sum()),
        "ranking": table.to_dict("records"),
        "not_ranked": [{"airport": r.airport, "passengers": r.passengers,
                        "reason": "below 500K passengers (no score)" if not r.in_peer_group else "no data"}
                       for r in unranked.itertuples()],
        **{k: metrics.METRICS[metric][k] for k in ("unit", "definition", "limitation")},
        "data": _sources(sources),
    }


@_traced
def airport_profile(airport: str) -> dict:
    """Deep dive on ONE airport (IATA code): traffic, congestion and forecast with US percentiles,
    all scores with ranks, the unmet-demand estimate (passengers/year with a range) and its
    drivers ranked by strength, cancellations and delay minutes by cause, assumptions.
    Use for 'how much unmet demand', 'why', and general questions about one airport."""
    return metrics.airport_profile(airport)


@_traced
def explain_metric(metric: str) -> dict:
    """Definition, unit, source and limitation of a metric; 'all' lists the whole catalog."""
    if metric == "all":
        return {"metrics": {m: d["definition"] for m, d in metrics.METRICS.items()}}
    if metric not in metrics.METRICS:
        return {"error": f"Unknown metric '{metric}'.", "known_metrics": list(metrics.METRICS)}
    return {"metric": metric, **metrics.METRICS[metric]}


ALL_TOOLS = [find_airports, get_metrics, rank_airports, airport_profile, explain_metric]
