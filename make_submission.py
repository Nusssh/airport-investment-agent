"""Builds submission/airport-agent.zip: code, data, tests and English docs. Never includes .env.

Run:  .\\.venv\\Scripts\\python.exe make_submission.py
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "submission" / "airport-agent.zip"
INCLUDE = ["app.py", "agent.py", "tools.py", "metrics.py", "grounding.py", "data_sources.py", "config.py",
           "requirements.txt", ".gitignore", "README.md", "DESIGN.md", "DESIGN.html", "data", "tests"]
SKIP = {"__pycache__", ".pytest_cache", ".env"}

if __name__ == "__main__":
    OUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for name in INCLUDE:
            path = ROOT / name
            for f in [path] if path.is_file() else sorted(path.rglob("*")):
                if f.is_file() and not (set(f.relative_to(ROOT).parts) & SKIP):
                    z.write(f, Path("airport-agent") / f.relative_to(ROOT))
    names = zipfile.ZipFile(OUT).namelist()
    assert not any(n.endswith(".env") for n in names), ".env must never be submitted"
    print(f"{OUT}  ({len(names)} files, {OUT.stat().st_size // 1024} KB, no .env)")
