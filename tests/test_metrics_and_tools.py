"""Automated tests of the deterministic layers (metrics, tools, groundedness check). No LLM, no network.

Run:  .\\.venv\\Scripts\\python.exe -m pytest tests -v

They prove that numbers are computed as documented, that the same input gives the same output,
and that bad input fails safely. They do not prove that the LLM phrases answers correctly
(see run_question_bank.py) or that the business assumptions are right.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import metrics  # noqa: E402
import tools  # noqa: E402
from grounding import ungrounded_numbers  # noqa: E402


@pytest.fixture(scope="module")
def table():
    df, sources = metrics.build_table(live=False)                      # cached data: no network
    metrics._cache.update({"table": df, "sources": sources})
    return df


def row(df, code):
    return df[df["airport"] == code].iloc[0]


# ---------- deterministic scoring ----------
def test_same_input_gives_same_scores(table):
    again, _ = metrics.build_table(live=False)
    for col in ["congestion_index", "unmet_demand_index", "expansion_index", "unmet_demand_passengers"]:
        pd.testing.assert_series_equal(table[col], again[col])


def test_weights_sum_to_one():
    for w in [config.CONGESTION_WEIGHTS, config.UNMET_DEMAND_WEIGHTS, config.MOMENTUM_WEIGHTS,
              config.EXPANSION_WEIGHTS]:
        assert abs(sum(w.values()) - 1) < 1e-9


# ---------- formulas match the documentation ----------
def test_congestion_index_is_mean_of_its_percentiles(table):
    r = row(table, "LAX")
    assert r["congestion_index"] == pytest.approx(
        (r["pctl_nas_delay"] + r["pctl_taxi_out"] + r["pctl_pct_delayed"]) / 3, abs=0.1)


def test_expansion_index_matches_formula(table):
    r = row(table, "BOS")
    assert r["expansion_index"] == pytest.approx(
        0.40 * r["unmet_demand_index"] + 0.35 * r["momentum_index"] + 0.25 * r["pctl_passengers"], abs=0.2)


def test_percentiles_only_for_peer_group(table):
    peers = table[table["in_peer_group"]]
    assert peers["pctl_passengers"].between(0, 100).all()
    assert table.loc[~table["in_peer_group"], "pctl_passengers"].isna().all()
    assert (peers["passengers"] >= config.PEER_MIN_PASSENGERS).all()


def test_small_airport_gets_no_score(table):
    r = row(table, "ACK")                                   # Nantucket: < 500K passengers
    assert not r["in_peer_group"] and pd.isna(r["expansion_index"])


def test_missing_inputs_flagged_through_all_layers(table):
    r = row(table, "HVN")                                   # no delay data for New Haven
    assert pd.notna(r["expansion_index"]) and "nas_delay" in r["expansion_index_missing"]


def test_known_value_sfo_has_highest_congestion_delay(table):
    assert row(table, "SFO")["pctl_nas_delay"] == 100.0


# ---------- unmet demand (spill model) ----------
def test_spill_grows_with_load_factor_and_variability():
    by_lf = [metrics.spill_per_seat(lf, 0.3) for lf in (60.0, 70.0, 80.0, 90.0)]
    assert by_lf == sorted(by_lf) and by_lf[0] < 0.01
    assert metrics.spill_per_seat(82.0, 0.2) < metrics.spill_per_seat(82.0, 0.3) < metrics.spill_per_seat(82.0, 0.4)


def test_unmet_demand_is_a_metric_for_all_airports_with_a_range(table):
    has = table[table["unmet_demand_passengers"].notna()]
    assert len(has) > 300
    assert (has["unmet_demand_low"] <= has["unmet_demand_passengers"]).all()
    assert (has["unmet_demand_passengers"] <= has["unmet_demand_high"]).all()


def test_drivers_only_when_above_national_median(table):
    drivers = metrics.airport_profile("SFO")["unmet_demand"]["drivers_ranked"]
    assert drivers and all(d["percentile_vs_us"] >= 50 for d in drivers)


# ---------- ranks are computed in code ----------
def test_rank_label_counts_correctly(table):
    label = metrics.rank_label(table, "expansion_index", "PVD")
    ne = table[(table["census_division"] == "New England") & table["expansion_index"].notna()]
    expected = int((ne["expansion_index"] > row(table, "PVD")["expansion_index"]).sum()) + 1
    assert f"{expected} of {len(ne)} in New England" in label


# ---------- tools: resolving names and failing safely ----------
def test_find_airports_resolves_city_names(table):
    found = tools.find_airports(["Santa Ana", "Los Angeles"])["matches"]      # several names, one call
    assert found["Santa Ana"][0]["airport"] == "SNA" and found["Los Angeles"][0]["airport"] == "LAX"


def test_short_query_does_not_match_inside_words(table):
    assert "ATL" not in [a["airport"] for a in tools.find_airports(["LA"])["matches"]["LA"]]


def test_ambiguous_city_returns_all_candidates(table):
    states = {a["state"] for a in tools.find_airports(["Portland"])["matches"]["Portland"]}
    assert {"ME", "OR"} <= states                           # the agent is told to ask which one


def test_rank_new_england_uses_census_division(table):
    r = tools.rank_airports("New England", "expansion_index")
    assert "Census" in r["scope"] and r["ranking"][0]["airport"] == "BOS" and r["ranking"][0]["position"] == 1
    assert any(x["airport"] == "ACK" for x in r["not_ranked"])


def test_unknown_inputs_fail_safely(table):
    r = tools.get_metrics(["XYZ", "LAX"], ["congestion_index", "happiness"])
    assert r["not_found"] == ["XYZ"] and r["unknown_metrics"] == ["happiness"]
    assert "error" in tools.rank_airports("Narnia", "growth")
    assert "error" in tools.rank_airports("US", "happiness")
    assert "error" in tools.airport_profile("XYZ")


def test_values_travel_with_limitations_and_ranks(table):
    r = tools.get_metrics(["ANC"], ["long_haul_share"])
    assert "domestic" in r["metrics"]["long_haul_share"]["limitation"]
    assert r["airports"][0]["long_haul_share_rank"].endswith("(highest first)")


# ---------- groundedness check ----------
TRACE = [{"result": {"central": 2025526, "low": 585102, "load_factor": 82.6, "growth": -4.4,
                     "rank": "6 of 7 in New England", "period": "2025-05 to 2026-04",
                     "ranking": [{"position": 1, "airport": "BOS"}, {"position": 2, "airport": "BTV"}]}}]


def test_grounding_accepts_rounded_values():
    assert ungrounded_numbers("About 2.0 million (from 590,000); 82.6% full, 83% rounded; 6th of 7.",
                              "q", TRACE) == []


def test_grounding_catches_invented_numbers_and_ranks():
    assert ungrounded_numbers("PVD is 5th with 12.4 minutes", "q", TRACE) == ["5th", "12.4"]


def test_grounding_ignores_list_markers_and_question_numbers():
    assert ungrounded_numbers("1. BOS is 1st\n2. BTV is 2nd; top 5 shown", "Show the top 5", TRACE) == []


def test_grounding_respects_signs():
    assert ungrounded_numbers("SAT shrank -4.4% (−4.4)", "q", TRACE) == []
    assert ungrounded_numbers("SAT grew 4.4%", "q", TRACE) == ["4.4"]      # sign error is caught


def test_grounding_handles_hebrew_text():
    assert ungrounded_numbers("כ-2.0 מיליון נוסעים, תפוסה 82.6%", "q", TRACE) == []


def test_grounding_treats_percentile_ordinals_as_values():
    trace = [{"result": {"pctl_nas_delay": 100.0, "pctl_load_factor": 90.8}}]
    assert ungrounded_numbers("100th percentile; 90.8th percentile", "q", trace) == []


def test_grounding_ignores_month_part_of_dates_with_any_hyphen():
    assert ungrounded_numbers("data 2025-05 to 2026‑04 and 2025–08", "q", TRACE) == []
