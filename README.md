# Airport Investment Intelligence Agent

A chat agent that helps analysts screen US airports for modernization opportunities. It ranks, compares and explains using public aviation data and deterministic scoring; the LLM interprets the question and writes the answer, and every number is checked against the data.

**🔗 Live demo: https://airport-investment-agent-nuss.streamlit.app** — no setup or API key needed.

Design (architecture, scoring methodology, tradeoffs, AI usage): [DESIGN.md](DESIGN.md)

## Run locally
```
pip install -r requirements.txt
echo GEMINI_API_KEY=your_key > .env        # free key: https://aistudio.google.com
streamlit run app.py
```
Optional: `GROQ_API_KEY` in `.env` enables a fallback model. Prepared data files are included in `data/`.

## Project structure
| File | Role |
|---|---|
| `app.py` | Chat UI |
| `agent.py` | LLM: tool calling, answer format, fallback provider |
| `tools.py` | The 5 tools the LLM can call |
| `metrics.py` | Metric definitions, scores, unmet-demand model (deterministic) |
| `grounding.py` | Checks every number in an answer against the tool results |
| `data_sources.py` | BTS API (live), data files, refresh, sanity checks |
| `config.py` | Weights, thresholds and other assumptions |
| `tests/` | Automated tests and a question bank |

## Tests
```
python -m pytest tests
```
