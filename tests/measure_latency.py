"""Where does the answer time go? Runs questions and splits total time into:
tools (our code + data APIs) vs. language model (incl. waiting for failed models).
Run:  .\\.venv\\Scripts\\python.exe tests\\measure_latency.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import metrics  # noqa: E402
from agent import Agent  # noqa: E402

QUESTIONS = [
    "Which airports in New England are strong candidates for terminal expansion?",
    "Compare LA and Santa Ana airport congestion levels.",
    "What is the percentage of long haul flights out of Anchorage airport?",
    "What is the unmet flight demand in SFO airport and why?",
]

if __name__ == "__main__":
    agent = Agent()
    if "--warm" in sys.argv:          # simulate the app pre-loading data at start-up
        t = time.time()
        metrics.get_table()
        print(f"warm-up (data load): {time.time() - t:.1f}s")
    print(f"{'question':45} {'total':>6} {'tools':>6} {'LLM':>6}  model / failed models")
    for q in QUESTIONS:
        r = agent.ask(q)
        tools_s = sum(c["seconds"] for c in r["trace"])
        failed = [e.split(":")[1] + ":" + e.split(":")[2].split()[0] for e in r["errors"]]
        print(f"{q[:45]:45} {r['seconds']:6.1f} {tools_s:6.1f} {r['seconds'] - tools_s:6.1f}  "
              f"{r['model']} {failed if failed else ''}")
