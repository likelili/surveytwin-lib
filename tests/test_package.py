"""The package stands alone: no website code, no web dependencies, no names."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import surveytwin as st

SRC = Path(st.__file__).resolve().parent
ROOT = SRC.parent.parent

WEB_MODULES = ("fastapi", "supabase", "posthog", "starlette", "uvicorn", "sqlalchemy", "psycopg", "asyncpg", "web", "services", "processing_qualtrics_qsf")


def test_import_pulls_in_no_web_modules():
    code = "import sys, surveytwin; print(sorted({m.split('.')[0] for m in sys.modules}))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    loaded = set(eval(out))  # noqa: S307 - our own subprocess output
    assert not loaded & set(WEB_MODULES)


def test_no_imports_of_website_packages():
    pattern = re.compile(r"^\s*(from|import)\s+(web|services|processing_qualtrics_qsf|fastapi|supabase|posthog)\b", re.M)
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


# Institution, lab, account and person names must not appear anywhere in the
# repository (it may be shared for double-blind review). The words are kept as
# hashes so this test does not itself list them. The Croissant Twins namespace
# and profile addresses belong to the standard and are allowed.
FORBIDDEN_HASHES = {
    "1e103fdeb16ccc3e", "a5bb9829c699828f", "9dd262afb5c02e76", "01261d1be29dcf2d", "de19c97d557a0e8a",
    "1009c42ac7c5b226", "4af48d7c76b521e9", "7e96fd638865d7d0", "b7825e8a22fd94dc", "71eeffb011a5f6f2",
    "16d87b796443a4c6", "152fc4dddcedc0be", "8920f499b6fa1d72", "445e02b873e53aac", "181056016443b23f",
}
ALLOWED = re.compile(r"(https?://)?[a-z.]*\.org/(ns|spec)/croissant-twins/?[\w./-]*", re.I)
TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.I)


def _tokens(text: str):
    for match in TOKEN.finditer(ALLOWED.sub(" ", text)):
        token = match.group(0).lower()
        yield token
        if "-" in token:
            yield from token.split("-")


def test_no_names_in_the_repository():
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in {".git", ".venv", "__pycache__", ".pytest_cache"} or part.endswith(".egg-info") for part in path.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".sqlite3", ".zip", ".pyc"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in set(_tokens(text)):
            if hashlib.sha256(token.encode()).hexdigest()[:16] in FORBIDDEN_HASHES:
                offenders.append(f"{path.relative_to(ROOT)} (a forbidden word)")
    assert sorted(set(offenders)) == []


def test_package_data_is_present():
    assert (SRC / "data" / "support_table.json").is_file()


def test_no_prices_anywhere():
    """Decided 2026-09-26: a library, not an app; it reports token counts only."""
    assert not (SRC / "data" / "prices.json").exists()
    assert not hasattr(st, "CostEstimate") and not hasattr(st.Plan, "estimate")
    assert not hasattr(st.Model, "price_tuple")
    from surveytwin.providers.usage import UsageRecord

    assert "cost_usd" not in UsageRecord.__dataclass_fields__
    code = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
    assert not re.search(r"cost_usd|price_table|list_prices|per_million_usd|prices=", code)


def test_the_license_is_apache_2():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.lstrip().startswith("Apache License") and "Version 2.0, January 2004" in text
    assert re.search(r'^license = "Apache-2.0"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)


def test_version():
    assert re.match(r"^\d+\.\d+\.\d+", st.__version__)
