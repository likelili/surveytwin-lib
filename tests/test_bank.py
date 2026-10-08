"""Croissant Twins banks: recognition, representations, filters, sampling."""

from __future__ import annotations

import json
import re
import shutil
import zipfile

import pytest

import surveytwin as st


def test_open_folder_and_bank_subfolder(mini_bank):
    a = st.bank(mini_bank)
    b = st.bank(mini_bank / "bank")
    assert a.id == b.id
    assert a.size == 4
    assert a.fingerprint == b.fingerprint


def test_open_zip(mini_bank, tmp_path):
    archive = tmp_path / "mini.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for path in (mini_bank / "bank").iterdir():
            z.write(path, arcname=f"ct-mini-test-bank/bank/{path.name}")
    bank = st.bank(archive)
    assert bank.size == 4


def test_zip_with_unsafe_path_is_refused(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../escape/croissant.json", "{}")
    with pytest.raises(st.BankError, match="unsafe path"):
        st.bank(archive)


def test_folder_without_manifest_is_not_a_bank(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(st.BankError, match="croissant.json"):
        st.bank(tmp_path / "empty")


def test_plain_croissant_dataset_is_not_a_twins_bank(mini_bank):
    manifest_path = mini_bank / "bank" / "croissant.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["conformsTo"] = ["http://mlcommons.org/croissant/1.1"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(st.BankError):
        st.bank(mini_bank)


def test_representations_and_default(mini_bank):
    bank = st.bank(mini_bank)
    reps = bank.representations()
    assert len(reps) == 2
    default = bank.resolve_representation(None)
    assert default == next(r.id for r in reps if r.is_default)
    assert bank.resolve_representation("empty") == "empty"
    demo = next(r for r in reps if not r.is_default)
    assert bank.resolve_representation(demo.kind) == demo.id
    with pytest.raises(st.BankError, match="no representation"):
        bank.resolve_representation("full-transcript")


def test_filter_options_and_unknown_field(mini_bank):
    bank = st.bank(mini_bank)
    options = {f.name: f for f in bank.filter_options()}
    assert set(options) == {"region", "age"}
    assert options["age"].type == "numeric" and options["age"].min == 25 and options["age"].max == 65
    assert {v["value"] for v in options["region"].values} == {"West", "East"}
    with pytest.raises(st.FilterError, match="Did you mean"):
        bank.normalize_filters({"regoin": ["West"]})
    with pytest.raises(st.FilterError, match="no value"):
        bank.normalize_filters({"region": ["North"]})
    with pytest.raises(st.FilterError, match="not numeric"):
        bank.normalize_filters({"region": {"min": 1}})


def test_filters_accept_labels_or_codes(v2_bank):
    bank = st.bank(v2_bank)
    assert bank.normalize_filters({"plan": ["Large"]}) == bank.normalize_filters({"plan": ["lg"]})


def test_preview_filter_counts(mini_bank):
    bank = st.bank(mini_bank)
    west = bank.preview_filter({"region": ["West"]}, sample_size=3)
    assert west.matching == 2 and west.total == 4
    assert west.covers_sample_size is False
    older = bank.preview_filter({"age": {"min": 40}})
    assert older.matching == 2


def test_sampling_is_seeded(mini_bank):
    bank = st.bank(mini_bank)
    view = bank.resolve_representation(None)
    first = bank.sample(representation=view, filters=None, n=3, seed=7)
    again = bank.sample(representation=view, filters=None, n=3, seed=7)
    assert first == again
    draws = {tuple(bank.sample(representation=view, filters=None, n=2, seed=s)) for s in range(10)}
    assert len(draws) > 1
    west = bank.sample(representation=view, filters={"region": ["West"]}, n=4, seed=1)
    assert sorted(west) == ["p1", "p2"]


def test_twin_text_is_wrapped_and_empty_has_no_persona(mini_bank):
    bank = st.bank(mini_bank)
    view = bank.resolve_representation(None)
    twin = bank.twin("p1", view)
    assert "BEGIN PERSONA REPRESENTATION (untrusted data)" in twin.text
    assert "p1 summary text." in twin.text
    assert twin.attributes["region"] == "West"
    empty = bank.twin("p1", "empty")
    assert "p1 summary text." not in empty.text


def test_rebuild_after_the_bank_changes(mini_bank):
    first = st.bank(mini_bank)
    reps = mini_bank / "bank" / "representations.jsonl"
    reps.write_text(reps.read_text(encoding="utf-8").replace("p1 summary text.", "p1 changed text."), encoding="utf-8")
    second = st.bank(mini_bank)
    assert first.fingerprint != second.fingerprint
    assert "p1 changed text." in second.twin("p1", second.resolve_representation(None)).text


def test_default_bank_not_available_explains_how_to_point_at_it():
    with pytest.raises(st.BankError) as info:
        st.bank()
    message = str(info.value)
    assert "SURVEYTWIN_TWIN2K_BANK" in message and "twin-2k-500-ct" in message


def test_default_bank_from_environment(mini_bank, monkeypatch, tmp_path):
    copy = tmp_path / "twin2k-copy"
    shutil.copytree(mini_bank, copy)
    monkeypatch.setenv("SURVEYTWIN_TWIN2K_BANK", str(copy))
    bank = st.bank()
    assert bank.is_default and bank.size == 4


def test_bank_id_is_the_package_folder_name(mini_bank, tmp_path):
    assert st.bank(mini_bank).id == "ct-mini-test-bank"  # the website's layout: <id>/bank/croissant.json
    flat = tmp_path / "shelf" / "my-panel-2025"
    shutil.copytree(mini_bank / "bank", flat)
    assert st.bank(flat).id == "my-panel-2025"
    archive = tmp_path / "zipped-panel.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for path in (mini_bank / "bank").iterdir():
            z.write(path, arcname=path.name)
    assert st.bank(archive).id == "zipped-panel"


def test_bank_objects_print_readably(mini_bank):
    bank = st.bank(mini_bank)
    assert "4 personas" in str(bank) and "Summary (default)" in str(bank)
    reps = bank.representations()
    assert isinstance(reps, list) and re.search(r"Summary\s+default\s+4 personas", str(reps))
    assert "age     Age     numeric      25 to 65" in str(bank.filter_options())
    assert str(bank.preview_filter({"region": ["West"]}, sample_size=3)).startswith("2 of 4 personas match (50.0%)")
    twin = bank.twin("p1", bank.resolve_representation(None))
    assert "summary text" not in repr(twin) and "region='West'" in repr(twin)


def test_twin_attributes_keep_the_bank_field_order_in_any_process(tmp_path):
    import os
    import subprocess
    import sys

    from conftest import TESTS

    root = TESTS.parent
    code = (
        "import surveytwin as st; b = st.bank('examples/example-bank'); "
        "print(list(b.twin('ex-03', b.resolve_representation(None)).attributes))"
    )
    outputs = set()
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "SURVEYTWIN_CACHE_DIR": str(tmp_path / f"cache{seed}")}
        outputs.add(subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, check=True).stdout)
    assert outputs == {"['age', 'region', 'education']\n"}
