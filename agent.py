"""Agent: the LLM understands the question, chooses tools and writes the answer.

Every number comes from tools (deterministic code). After each answer a groundedness check
verifies that the numbers in it appear in the tool results; if not, the model gets one chance
to correct itself, and any remaining unverified numbers are reported to the UI.

Model routing: config.LLM_CHAIN mixes two providers (Gemini, Groq) so that a provider-wide
outage does not take the agent down. Models that failed recently are tried last.
"""
import inspect
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

import config
import tools
from grounding import ungrounded_numbers

SYSTEM_PROMPT = """You answer questions from analysts at a firm investing in US airport modernization.
The analyst decides; you give clear answers backed by public data.

RULES
- Every number, rank and score must come from a tool result. Never calculate or infer numbers,
  including positions like "5th" - use the rank/position fields the tools return.
- Resolve places with find_airports, all names in one call (e.g. "LA" -> Los Angeles -> LAX). If a city matches several
  airports and context does not decide, ask which one. Regions like "New England" are US Census
  divisions: pass them as the scope of rank_airports.
- Metric catalog (names for get_metrics / rank_airports): {catalog}.
  Call explain_metric only if the user asks what a metric means. Typical mapping:
  expansion candidates -> expansion_index; congestion -> congestion_index (+ its 3 components);
  how much unmet demand / why -> airport_profile (unmet_demand + drivers_ranked);
  rankings of unmet demand -> rank_airports(metric="unmet_demand_passengers").
- If no metric fits the question, say what data is missing instead of guessing.
- Scores (0-100) are not percentiles: write "index 92.6", never "92.6th percentile".
- Facts you add that no tool returned must be marked "(general knowledge, not from the data)".

ANSWER FORMAT (short; the analyst can ask for more)
1. Bottom line: 1-2 sentences with the key number(s). For estimates give the central value and range.
2. Why: 2-3 bullets in plain language, each with its number. For unmet demand use drivers_ranked
   in order; do not add other drivers.
3. One line "Assumptions & limits" with what matters for this answer (period, coverage, partial
   scores, ambiguous terms and how you interpreted them).
Rankings: a compact markdown table (max 6 rows). Round large numbers ("about 2.0 million").

Answer in the language of the question.

SCOPE: US airports only. No financial returns or investment recommendations - when asked, say so
briefly and offer what you can provide.
"""

SYSTEM_PROMPT = SYSTEM_PROMPT.replace("{catalog}", ", ".join(tools.metrics.METRICS))

CORRECTION = ("Your answer contains numbers that do not appear in any tool result: {bad}. "
              "Rewrite the answer using only numbers returned by the tools (call tools again if needed).")

TOOLS_BY_NAME = {fn.__name__: fn for fn in tools.ALL_TOOLS}
FAILED_AT: dict[str, float] = {}     # model -> time of its last failure (shared by all sessions)


def _secret(name: str) -> str | None:
    """API key from the environment / .env, or from Streamlit secrets when deployed."""
    if os.getenv(name):
        return os.getenv(name)
    try:
        import streamlit as st
        return st.secrets.get(name)
    except Exception:
        return None


def _openai_tool_schema(fn) -> dict:
    """JSON description of a Python tool for OpenAI-compatible APIs (Groq)."""
    types_map = {str: "string", int: "integer", bool: "boolean", float: "number"}
    props, required = {}, []
    for name, p in inspect.signature(fn).parameters.items():
        props[name] = ({"type": "array", "items": {"type": "string"}} if p.annotation == list[str]
                       else {"type": types_map.get(p.annotation, "string")})
        if p.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "function", "function": {
        "name": fn.__name__, "description": inspect.getdoc(fn),
        "parameters": {"type": "object", "properties": props, "required": required}}}


class Agent:
    def __init__(self):
        load_dotenv(Path(__file__).parent / ".env")       # local runs
        self.keys = {"gemini": _secret("GEMINI_API_KEY"), "groq": _secret("GROQ_API_KEY")}
        if not any(self.keys.values()):
            raise RuntimeError("No API key found - add GEMINI_API_KEY and/or GROQ_API_KEY to .env (see README).")
        if self.keys["gemini"]:
            self.gemini = genai.Client(api_key=self.keys["gemini"], http_options=types.HttpOptions(
                timeout=config.LLM_TIMEOUT_SECONDS * 1000, retry_options=types.HttpRetryOptions(attempts=1)))
        self.turns: list[dict] = []        # provider-neutral history: follow-ups survive a provider switch

    def _route(self) -> list[tuple[str, str]]:
        """Models with a key, in configured order; models that failed in the last
        config.MODEL_COOLDOWN_SECONDS move to the end (never removed, so something is always tried)."""
        usable = [(p, m) for p, m in config.LLM_CHAIN if self.keys.get(p)]
        cooling = lambda m: time.time() - FAILED_AT.get(m, 0) < config.MODEL_COOLDOWN_SECONDS
        return [x for x in usable if not cooling(x[1])] + [x for x in usable if cooling(x[1])]

    def ask(self, question: str) -> dict:
        """Returns answer, model, tool trace, provider errors, unverified numbers and timing."""
        tools.TRACE.clear()
        errors, started = [], time.time()
        for provider, model in self._route():
            call = self._ask_gemini if provider == "gemini" else self._ask_groq
            try:
                answer = call(model, self.turns, question)
                bad = ungrounded_numbers(answer, question, tools.TRACE)
                if bad:                                   # one correction round, same model
                    retry_history = self.turns + [{"role": "user", "content": question},
                                                  {"role": "assistant", "content": answer}]
                    answer = call(model, retry_history, CORRECTION.format(bad=", ".join(bad)))
                    bad = ungrounded_numbers(answer, question, tools.TRACE)
                self.turns += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
                return {"answer": answer, "model": f"{provider}:{model}", "trace": list(tools.TRACE),
                        "errors": errors, "unverified_numbers": bad, "seconds": round(time.time() - started, 1)}
            except Exception as e:                        # 404 retired, 429 quota, 503/504 overload, timeout
                errors.append(f"{provider}:{model}: {type(e).__name__}: {str(e)[:160]}")
                FAILED_AT[model] = time.time()
        return {"answer": "The language model services are unavailable right now (free-tier limits or "
                          "provider load). Please try again in a minute.",
                "model": None, "trace": list(tools.TRACE), "errors": errors, "unverified_numbers": [],
                "seconds": round(time.time() - started, 1)}

    # ---- Gemini: the SDK runs the tool-calling loop ----
    def _ask_gemini(self, model: str, history: list[dict], message: str) -> str:
        contents = [types.Content(role="user" if t["role"] == "user" else "model",
                                  parts=[types.Part.from_text(text=t["content"])])
                    for t in history[-config.HISTORY_MESSAGES:]]
        cfg = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT, tools=tools.ALL_TOOLS, temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=12))
        text = self.gemini.chats.create(model=model, config=cfg, history=contents).send_message(message).text
        if not text:
            raise RuntimeError("empty answer")
        return text

    # ---- Groq (OpenAI-compatible): we run the tool-calling loop ----
    def _ask_groq(self, model: str, history: list[dict], message: str) -> str:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history[-config.HISTORY_MESSAGES:],
                    {"role": "user", "content": message}]
        schemas = [_openai_tool_schema(fn) for fn in tools.ALL_TOOLS]
        for _ in range(12):
            msg = self._groq_post(model, messages, schemas).json()["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                if not msg.get("content"):
                    raise RuntimeError("empty answer")
                return msg["content"]
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:                               # run each requested tool, send the result back
                fn = TOOLS_BY_NAME.get(c["function"]["name"])
                try:
                    result = fn(**json.loads(c["function"]["arguments"] or "{}")) if fn else {"error": "unknown tool"}
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {e}"}
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": json.dumps(result, default=str, separators=(",", ":"))[:20000]})
        raise RuntimeError("too many tool-call rounds")

    def _groq_post(self, model: str, messages: list, schemas: list) -> requests.Response:
        """On a per-minute limit Groq answers 429 'try again in 9.4s': wait once if that is short."""
        for attempt in range(2):
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions",
                                 headers={"Authorization": f"Bearer {self.keys['groq']}"},
                                 json={"model": model, "messages": messages, "tools": schemas, "temperature": 0.2},
                                 timeout=config.LLM_TIMEOUT_SECONDS)
            wait = re.search(r"try again in ([\d.]+)s", resp.text) if resp.status_code == 429 else None
            if attempt == 0 and wait and float(wait.group(1)) <= config.GROQ_MAX_WAIT_SECONDS:
                time.sleep(float(wait.group(1)) + 0.5)
                continue
            resp.raise_for_status()
            return resp


if __name__ == "__main__":
    # Terminal use: python agent.py "question" ["follow-up" ...]
    sys.stdout.reconfigure(encoding="utf-8")
    agent = Agent()
    for q in sys.argv[1:] or ["What is the unmet flight demand in SFO airport and why?"]:
        r = agent.ask(q)
        print("=" * 80, f"\nQ: {q}\n[{r['model']} | {r['seconds']}s | tools: {[t['tool'] for t in r['trace']]}"
              f"{' | unverified: ' + str(r['unverified_numbers']) if r['unverified_numbers'] else ''}]")
        for e in r["errors"]:
            print("  fallback:", e[:120])
        print(r["answer"], flush=True)
