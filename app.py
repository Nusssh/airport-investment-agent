"""Chat UI (Streamlit). Presentation only - logic lives in agent.py, tools.py and metrics.py.

Run:  .\\.venv\\Scripts\\python.exe -m streamlit run app.py
"""
import streamlit as st

import data_sources as ds
import metrics
from agent import Agent

st.set_page_config(page_title="Airport Investment Intelligence", page_icon="✈️", layout="wide")

# st.session_state persists across Streamlit reruns (the script reruns on every interaction).
if "agent" not in st.session_state:
    st.session_state.agent = Agent()
    st.session_state.messages = []                      # [{"role", "content", "details"}]
    with st.spinner("Loading live data from the BTS API..."):
        metrics.get_table()                             # pre-load so the first question doesn't wait


# ---------------- sidebar: data provenance and scope ----------------
with st.sidebar:
    st.header("📡 Data sources")
    meta = ds.load_meta()
    traffic = meta.get("traffic", {})
    _, sources = metrics.get_table()
    live = sources["traffic"].get("mode") == "live"
    st.markdown(f"{'🟢' if live else '🟡'} **BTS T-100 API** – {'live' if live else 'saved copy (API unavailable)'}  \n"
                f"period {traffic.get('period', '?')} · fetched {traffic.get('fetched_at', '?')} UTC"
                f" ({traffic.get('seconds', '?')}s)")
    st.markdown(f"📁 **BTS delays** – files  \n{sources['delays'].get('period')} · large US airlines, domestic")
    st.markdown(f"📁 **FAA forecast (TAF)** – edition {meta.get('taf', {}).get('edition', '?')}")
    st.markdown("📁 **Airports** – OurAirports + US Census regions")
    if st.button("🔄 Refresh data", help="Re-queries the API and downloads only new files"):
        with st.spinner("Checking for new data..."):
            warnings = ds.refresh_all(progress=lambda msg: None)
            metrics.refresh_table()
        st.session_state.refresh_msg = ("Up to date." if not warnings
                                        else "Done with warnings:\n- " + "\n- ".join(warnings))
        st.rerun()                                      # redraw the sidebar with the new fetch time
    if "refresh_msg" in st.session_state:
        st.success(st.session_state.pop("refresh_msg"))

    st.divider()
    st.header("📏 Scope & assumptions")
    st.caption("US commercial airports · last 12 months of published data · scores (0–100) are based "
               "on percentiles vs. US airports with 500K+ passengers · congestion is measured through "
               "delays, not physical capacity · unmet demand is a model estimate with a range · "
               "the agent provides evidence; the analyst decides.")
    if st.button("🗑️ New conversation"):
        st.session_state.agent = Agent()
        st.session_state.messages = []
        st.rerun()


# ---------------- conversation ----------------
st.title("✈️ Airport Investment Intelligence Agent")
st.caption("Ask about US airports: expansion candidates, congestion, traffic, unmet demand. "
           "Numbers come from public data and deterministic code; the AI explains them.")


def describe(call: dict) -> str:
    """One plain-language line per tool call, for analysts."""
    a, r = call["args"], call["result"] or {}
    if not isinstance(a, dict):                         # positional call: map to parameter names
        names = {"find_airports": ["queries"], "get_metrics": ["airports", "metric_names"],
                 "rank_airports": ["scope", "metric"], "airport_profile": ["airport"], "explain_metric": ["metric"]}
        a = dict(zip(names.get(call["tool"], []), a))
    if call["tool"] == "find_airports":
        found = [f"{q} → {m[0]['airport']}" if len(m) == 1 else f"{q} → {len(m)} possible airports"
                 for q, m in r.get("matches", {}).items() if m]
        return "Searched airports: " + (", ".join(found) or "no match")
    if call["tool"] == "get_metrics":
        return (f"Looked up {', '.join(m.replace('_', ' ') for m in a.get('metric_names', []))} "
                f"for {', '.join(a.get('airports', []))}")
    if call["tool"] == "rank_airports":
        return f"Ranked {r.get('scope', a.get('scope'))} by {a.get('metric', '').replace('_', ' ')}"
    if call["tool"] == "airport_profile":
        return (f"Pulled the full profile of {a.get('airport')}: traffic, congestion, "
                f"unmet-demand estimate and its drivers")
    return f"Looked up the definition of {a.get('metric', '').replace('_', ' ')}"


def show_details(details: dict):
    with st.expander("🔍 How this answer was built"):
        if details["trace"]:
            st.markdown("\n".join(f"{i}. {describe(c)}" for i, c in enumerate(details["trace"], 1)))
            _, sources = metrics.get_table()
            st.markdown(f"**Data:** traffic {sources['traffic'].get('period')} (BTS, live API) · "
                        f"delays {sources['delays'].get('period')} (BTS) · "
                        f"forecast FAA TAF {sources['faa_forecast'].get('edition')}")
        if details.get("unverified_numbers"):
            st.warning("Could not verify these numbers against the data: " + ", ".join(details["unverified_numbers"]))
        elif details["trace"]:
            st.success("Every number in the answer was checked against the data.")
        st.caption(f"Answered in {details['seconds']}s by {details['model']}")
        if st.toggle("Show technical details", key=f"tech{id(details)}"):
            if details["errors"]:
                st.info("Fallbacks used:\n- " + "\n- ".join(details["errors"]))
            for i, call in enumerate(details["trace"], 1):
                st.markdown(f"**{i}. `{call['tool']}`** ({call['seconds']}s) – `{call['args']}`")
                st.json(call["result"], expanded=False)


for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("details"):
            show_details(msg["details"])

question = st.chat_input("Ask a question about US airports...")
if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("Pulling data and thinking..."):
            result = st.session_state.agent.ask(question)
        st.markdown(result["answer"])
        details = {k: result[k] for k in ("model", "seconds", "errors", "trace", "unverified_numbers")}
        show_details(details)
    st.session_state.messages.append({"role": "assistant", "content": result["answer"], "details": details})
