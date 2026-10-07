"""Tokens and LLM rounds per question (measured on Groq, which reports exact usage per call).

Run:  .\\.venv\\Scripts\\python.exe tests\\measure_tokens.py
Use it to compare before/after a change; absolute numbers differ slightly between models.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import agent  # noqa: E402
import config  # noqa: E402
import metrics  # noqa: E402

QUESTIONS = [
    "Which airports in New England are strong candidates for terminal expansion?",
    "Compare LA and Santa Ana airport congestion levels.",
    "What is the percentage of long haul flights out of Anchorage airport?",
    "What is the unmet flight demand in SFO airport and why?",
]

if __name__ == "__main__":
    config.LLM_CHAIN = [("groq", "openai/gpt-oss-120b")]
    usage = []
    original = agent.Agent._groq_post

    def counting_post(self, model, messages, schemas):
        resp = original(self, model, messages, schemas)
        usage.append(resp.json()["usage"]["total_tokens"])
        return resp

    agent.Agent._groq_post = counting_post
    metrics.get_table()
    print(f"{'question':45} {'rounds':>6} {'tokens':>7} {'tools'}")
    total = 0
    for q in QUESTIONS:
        usage.clear()
        a = agent.Agent()                      # fresh conversation: compare questions independently
        r = a.ask(q)
        total += sum(usage)
        print(f"{q[:45]:45} {len(usage):6} {sum(usage):7}  {[t['tool'] for t in r['trace']]}"
              f"{'  FAILED' if not r['model'] else ''}", flush=True)
        time.sleep(20)                         # stay under the free tier's tokens-per-minute limit
    print(f"{'TOTAL':45} {'':6} {total:7}")
