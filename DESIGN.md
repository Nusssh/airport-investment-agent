# Airport Investment Intelligence Agent – Design

## 1. Architecture

```
Analyst
  │
app.py ............ Streamlit chat UI; shows data periods and how each answer was built
  │
agent.py .......... LLM (Gemini 3.5 Flash-Lite; Groq GPT-OSS-120B fallback if its key is set)
  │   ↕ grounding.py  every number in the answer must appear in the tool results
  │
tools.py .......... find_airports · get_metrics · rank_airports · airport_profile · explain_metric
  │
metrics.py ........ all metric definitions and calculations (deterministic, no LLM)
  │
data_sources.py ... BTS T-100 REST API (live, at start-up and on Refresh; server-side aggregation)
                    BTS On-Time files · FAA Terminal Area Forecast · OurAirports + US Census regions
```

**Example – "Compare LA and Santa Ana congestion":**
1. The LLM calls `find_airports(["Los Angeles", "Santa Ana"])` → LAX, SNA.
2. The LLM calls `get_metrics(["LAX","SNA"], ["congestion_index", …])` → the code returns values, ranks, definitions and limitations.
3. The LLM writes the answer; `grounding.py` verifies every number; the UI lists the steps and data periods.

## 2. Scoring methodology

All metrics use the last 12 months of published data. Scores (0–100) are **percentiles against the 141 US airports with 500K+ passengers**, so an airport has the same score in every question and outliers don't distort the scale.

```
congestion_index   = mean percentile of: NAS delay per arrival, taxi-out time, % flights delayed 15+ min
unmet_demand_index = 0.50 × pct(load factor) + 0.50 × congestion_index
momentum_index     = 2/3 × pct(passenger growth) + 1/3 × pct(FAA 5-year forecast)
expansion_index    = 0.40 × unmet_demand_index + 0.35 × momentum_index + 0.25 × pct(passengers)
```

- **Expansion weights:** capacity pays where demand is already unmet (40%), keeps growing (35%) and is large (25%). Sensitivity check over 5 weight sets: Boston stays first in New England in 4 of 5; ORD and SFO stay top-2 nationally in all 5.
- **Unmet demand as a number:** passengers who wanted a seat but flights were full. A spill model (daily demand ~ Normal) is fitted to each month's load factor; day-to-day variability is unknown, so the result is a range. SFO ≈ 2.0M passengers/year (0.59M–4.7M), driven by the highest congestion delay in the US and an 82.6% load factor.
- **Missing data:** a score uses the inputs that exist and is flagged *partial*; airports under 500K passengers get metrics but no score.

## 3. Key tradeoffs

| Decision | Alternative | Cost |
|---|---|---|
| Live API pull at start-up and on Refresh | Pull on every question | Data is as of the last refresh (BTS publishes monthly; a pull takes 2–20s) |
| Percentiles vs. a national peer group | Min-max scaling | Loses the size of gaps → answers show raw values next to scores |
| 5 general tools, combined by the LLM | A fixed flow per question type | The LLM can pick the wrong tool → tool trace and automated checks |
| One model + fallback from a second provider | A single model | Two integrations; fallback runs only if its key is configured |
| Congestion measured by delays | Physical capacity (FAA ASPM) | ASPM needs registration; delays show effects, not capacity |
| Ambiguous signals kept out of scores (fares, planned spend) | Add them to the index | Not used yet; the agent says the data is unavailable. Next step: show them as context with both readings |

## 4. Where/how AI is used

| LLM | Code | External data |
|---|---|---|
| Understands the question; maps names ("LA" → LAX, "New England" → Census division) | Every number, score, rank and estimate | BTS, FAA, OurAirports, US Census |
| Chooses which tools to call | Metric definitions and limitations | |
| Writes the answer: bottom line → 2–3 reasons → assumptions | Checks that each number in the answer appears in the tool results | |

**Guardrails:** numbers only from tools; ranks computed in code; if the answer contains a number not found in the tool results, the LLM gets one correction round and anything left is flagged in the UI; out-of-scope requests (ROI, non-US airports) are declined.
