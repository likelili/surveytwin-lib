from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
QSF_DIR = TESTS / "fixtures" / "qsf"
sys.path.insert(0, str(TESTS))

from banks import write_mini_bank, write_v2_bank  # noqa: E402


def qsf(name: str) -> Path:
    return QSF_DIR / name


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path_factory):
    """No real keys, no user cache, no default-bank settings leak into a test."""
    for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "SURVEYTWIN_TWIN2K_BANK", "SURVEYTWIN_TWIN2K_HF_REPO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SURVEYTWIN_CACHE_DIR", str(tmp_path_factory.getbasetemp() / "cache"))


@pytest.fixture
def mini_bank(tmp_path) -> Path:
    write_mini_bank(tmp_path)
    return tmp_path / "ct-mini-test-bank"


@pytest.fixture
def v2_bank(tmp_path) -> Path:
    write_v2_bank(tmp_path)
    return tmp_path / "ct-v2-test-bank"
