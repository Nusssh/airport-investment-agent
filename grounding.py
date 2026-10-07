"""Groundedness check: every number in an answer must come from the tool results (or the question).

Catches a whole class of LLM errors - invented numbers, inferred ranks ("5th"), own arithmetic -
instead of patching each case in the prompt. Rounding is allowed ("2.0 million" matches 2,025,526).
Ordinals ("5th") must match a position or rank returned by a tool.
"""
import re

_DATE = re.compile(r"\b(\d{4})[-‐‑–]\d{2}\b")                       # "2025-05" -> only the year counts
# a sign counts only when not glued to a word ("-4.4%" is negative; Hebrew "כ-37" means "about 37")
_NUMBER = re.compile(r"(?<![\w.])([-−+]?\d[\d,]*(?:\.\d+)?)\s*"
                     r"(million|billion|thousand|מיליון|מיליארד|אלף|[MKBk](?![a-zA-Z]))?")
_ORDINAL = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b(?!\s*percentile)")       # "100th percentile" is a value
_RANK_TEXT = re.compile(r"\b(\d+) of \d+")                      # rank labels like "6 of 7 in New England"
_SCALE = {"million": 1e6, "m": 1e6, "billion": 1e9, "b": 1e9, "thousand": 1e3, "k": 1e3,
          "מיליון": 1e6, "מיליארד": 1e9, "אלף": 1e3}
_ALWAYS_OK = {0.0, 100.0}                                       # bounds of the 0-100 scores


def _numbers_in(text: str) -> list[tuple[str, float, float, bool]]:
    """(token, value, rounding tolerance, is_ordinal) for each number, ignoring list markers ('1. ')."""
    text = _DATE.sub(r"\1", re.sub(r"(?m)^\s*\d+\.\s", " ", text))
    ordinals = {m.start(1) for m in _ORDINAL.finditer(text)}
    out = []
    for m in _NUMBER.finditer(text):
        raw, unit = m.group(1).replace(",", "").replace("−", "-").lstrip("+"), (m.group(2) or "").lower()
        try:
            value = float(raw)
        except ValueError:
            continue
        if unit:                                    # "2.0 million": scaled numbers are rounded
            tol = 0.05 * abs(value) * _SCALE.get(unit, 1)
        elif "." in raw:                            # "82.6" -> +-0.05
            tol = 0.5 * 10 ** -len(raw.split(".")[1])
        else:                                       # "590,000" -> +-5,000 ; "83" -> +-0.5
            digits = raw.lstrip("-")
            zeros = len(digits) - len(digits.rstrip("0")) if value else 0
            tol = 0.5 * 10 ** zeros
        out.append((m.group(0).strip(), value * _SCALE.get(unit, 1), tol, m.start(1) in ordinals))
    return out


def _collect(obj, values: set, positions: set, key: str = ""):
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        values.add(float(obj))
        if key == "position":
            positions.add(float(obj))
    elif isinstance(obj, str):
        values.update(v for _, v, _, _ in _numbers_in(obj))
        positions.update(float(n) for n in _RANK_TEXT.findall(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _collect(v, values, positions, str(k))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect(v, values, positions, key)


def ungrounded_numbers(answer: str, question: str, trace: list[dict]) -> list[str]:
    """Numbers in the answer that match no tool result within the rounding the answer uses."""
    values, positions = set(_ALWAYS_OK), set()
    for call in trace:
        _collect(call.get("result"), values, positions)
    _collect(question, values, positions)
    positions |= {float(n) for n in re.findall(r"\d+", question)}

    bad = []
    for token, x, tol, is_ordinal in _numbers_in(answer):
        pool = positions if is_ordinal else values
        if not any(abs(x - v) <= tol + 1e-9 for v in pool):
            bad.append(token + ("th" if is_ordinal and not token.endswith("th") else ""))
    return bad
