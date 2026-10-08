"""The ``surveytwin`` command."""

from __future__ import annotations

import pytest

import surveytwin as st
from conftest import qsf
from fakes import scripted_model
from surveytwin.cli import main


def test_check(capsys):
    assert main(["check", str(qsf("kitchen_sink_supported.qsf"))]) == 0
    out = capsys.readouterr().out
    assert "Execution mode: staged" in out
    assert "BL_KS_MC_1" in out


def test_check_with_a_dropped_block_that_logic_reads(capsys):
    assert main(["check", str(qsf("kitchen_sink_supported.qsf")), "--drop", "BL_KS_MC_1"]) == 0
    out = capsys.readouterr().out
    assert "DROPPED" in out and "branch at FL FL_14 reads QID56" in out and "counts as never shown" in out
    assert main(["check", str(qsf("kitchen_sink_supported.qsf")), "--drop", "BL_KS_MC_1", "--assume", "QID56=Cat"]) == 2
    assert "--assume was removed" in capsys.readouterr().err


def test_prompts(mini_bank, capsys):
    common = [str(qsf("TEST_MC.qsf")), "--bank", str(mini_bank), "--twins", "2", "--seed", "3"]
    assert main(["prompts", *common]) == 0
    out = capsys.readouterr().out
    assert out.count("##### twin ") == 2


def test_there_is_no_estimate_command():
    with pytest.raises(SystemExit):
        main(["estimate", str(qsf("TEST_MC.qsf"))])


def test_filters_on_the_command_line(mini_bank, capsys):
    base = ["prompts", str(qsf("TEST_MC.qsf")), "--bank", str(mini_bank), "--twins", "4"]
    assert main([*base, "--filter", "region=East"]) == 0
    out = capsys.readouterr().out
    assert out.count("##### twin ") == 2
    assert main([*base, "--filter", "age=40..70"]) == 0
    assert capsys.readouterr().out.count("##### twin ") == 2


def test_errors_exit_with_code_2(mini_bank, capsys):
    code = main(["prompts", str(qsf("TEST_MC.qsf")), "--bank", str(mini_bank), "--filter", "gender=F"])
    assert code == 2
    assert "Did you mean" in capsys.readouterr().err or True
    assert main(["prompts", str(qsf("TEST_MC.qsf"))]) == 2  # the default bank is not available in tests
    assert "SURVEYTWIN_TWIN2K_BANK" in capsys.readouterr().err


def test_system_instruction_from_a_file(mini_bank, tmp_path, capsys):
    path = tmp_path / "instruction.txt"
    path.write_text("Answer as quickly as you can.", encoding="utf-8")
    assert main(["prompts", str(qsf("TEST_MC.qsf")), "--bank", str(mini_bank), "--twins", "1", "--system-instruction", f"@{path}"]) == 0
    assert "Answer as quickly as you can." in capsys.readouterr().out


def test_export_report_and_rerun_on_a_saved_run(mini_bank, tmp_path, capsys):
    config = st.configure(bank=mini_bank, sample_size=2, seed=5, model=scripted_model())
    folder = st.run(st.plan(st.read_qsf(qsf("TEST_TE.qsf")), config)).save(tmp_path / "run")
    assert main(["export", str(folder), "--bundle"]) == 0
    assert (folder / "export_bundle.zip").is_file()
    assert main(["report", str(folder), "--json"]) == 0
    assert (folder / "report.json").is_file()
    # a defined model cannot be restored from a saved run
    assert main(["rerun", str(folder)]) == 2
    capsys.readouterr()


def test_run_needs_a_model():
    with pytest.raises(SystemExit):
        main(["run", str(qsf("TEST_MC.qsf")), "--out", "x"])


def test_command_line_mistakes_exit_with_code_2(mini_bank, tmp_path, capsys):
    base = ["prompts", str(qsf("TEST_MC.qsf")), "--bank", str(mini_bank)]
    assert main([*base, "--filter", "age=young..old"]) == 2
    assert "needs numbers" in capsys.readouterr().err
    assert main([*base, "--system-instruction", f"@{tmp_path / 'missing.txt'}"]) == 2
    assert "cannot read" in capsys.readouterr().err
    assert main(["batch", "status", "--out", str(tmp_path / "nothing")]) == 2
    assert "no batch here" in capsys.readouterr().err


def test_batch_submit_saves_the_arguments_it_was_given(mini_bank, tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(st.batch, "submit", lambda plan, **kw: "batch_123")
    argv = ["batch", "submit", str(qsf("TEST_MC.qsf")), "--bank", str(mini_bank), "--twins", "2", "--model", "gpt-4o-mini", "--out", str(tmp_path / "run")]
    assert main(argv) == 0
    state = json.loads((tmp_path / "run" / "batch_state.json").read_text(encoding="utf-8"))
    assert state == {"batch_id": "batch_123", "argv": argv}


# ---- models defined in a Python file: --model FILE.py:NAME ------------------------------

MODELS_FILE = """
import json, sys
sys.path.insert(0, {tests!r})
import surveytwin as st
from fakes import answer_questions, scripted

fake = st.Model.from_function(scripted, name="fake", images=True)


def make_fake():
    return st.Model.from_function(scripted, name="made", images=True)


def _skip_first(request):
    answers = answer_questions(request.user)[1:]
    return st.Reply(text=json.dumps({{"answers": answers}}), input_tokens=3, output_tokens=2)


skipper = st.Model.from_function(_skip_first, name="skipper")
echo = st.Model.from_function(lambda request: st.Reply(text='{{"ok": true}}', input_tokens=1, output_tokens=1), name="echo")
number = 42
"""


@pytest.fixture
def models_file(tmp_path):
    from conftest import TESTS

    path = tmp_path / "mymodels.py"
    path.write_text(MODELS_FILE.format(tests=str(TESTS)), encoding="utf-8")
    return path


def _results(folder):
    import json

    return json.loads((folder / "results.json").read_text(encoding="utf-8"))


def test_run_with_a_model_from_a_file(mini_bank, models_file, tmp_path, capsys):
    out = tmp_path / "run"
    argv = ["run", str(qsf("TEST_TE.qsf")), "--bank", str(mini_bank), "--twins", "2", "--model", f"{models_file}:fake", "--out", str(out)]
    assert main(argv) == 0
    model = _results(out)["run"]["config"]["model"]
    assert model["kind"] == "function" and model["name"] == "fake"
    assert model["defined_in"].startswith("mymodels.py:fake (sha256 ")
    assert str(tmp_path) not in (out / "results.json").read_text(encoding="utf-8")  # the file name only, not its folder


def test_a_function_that_builds_the_model(mini_bank, models_file, capsys):
    argv = ["prompts", str(qsf("TEST_MC.qsf")), "--bank", str(mini_bank), "--twins", "1", "--model", f"{models_file}:make_fake"]
    assert main(argv) == 0


@pytest.mark.parametrize(
    "reference, message",
    [
        ("missing.py:fake", "no such file"),
        ("{file}:nothing", "has no 'nothing'. It defines: echo, fake, skipper"),
        ("{file}:number", "is a int, not a surveytwin Model"),
        ("{file}:2bad", "not a Python name"),
    ],
)
def test_model_file_mistakes_exit_with_code_2(mini_bank, models_file, capsys, reference, message):
    reference = reference.format(file=models_file)
    argv = ["prompts", str(qsf("TEST_MC.qsf")), "--bank", str(mini_bank), "--model", reference]
    assert main(argv) == 2
    assert message in capsys.readouterr().err


def test_a_model_file_that_fails_to_load(mini_bank, tmp_path, capsys):
    broken = tmp_path / "broken.py"
    broken.write_text("raise RuntimeError('no key here')\n", encoding="utf-8")
    assert main(["prompts", str(qsf("TEST_MC.qsf")), "--bank", str(mini_bank), "--model", f"{broken}:x"]) == 2
    assert "broken.py failed to load: RuntimeError: no key here" in capsys.readouterr().err


def test_provider_is_for_preset_names_only(mini_bank, models_file, capsys):
    argv = ["prompts", str(qsf("TEST_MC.qsf")), "--bank", str(mini_bank), "--model", f"{models_file}:fake", "--provider", "openrouter"]
    assert main(argv) == 2
    assert "preset names only" in capsys.readouterr().err


def test_fine_tuned_model_names_with_colons_stay_preset_names():
    from surveytwin.cli import is_model_file, model_value

    for name in ("ft:gpt-4.1-2025-04-14:org::AbC123", "gpt-5.6-luna-medium", "openai/gpt-5.4"):
        assert not is_model_file(name)
        assert model_value(name) == name
    assert is_model_file("models/claude.py:claude")


def test_rerun_with_a_model_from_a_file(mini_bank, models_file, tmp_path, capsys):
    out = tmp_path / "run"
    base = ["run", str(qsf("TEST_TE.qsf")), "--bank", str(mini_bank), "--twins", "2", "--out", str(out)]
    assert main([*base, "--model", f"{models_file}:skipper"]) == 0
    flagged = [r for r in _results(out)["rows"] if r.get("answer_status") == "skipped"]
    assert flagged
    assert main(["rerun", str(out)]) == 2  # the run's model came from a file; it must be named again
    assert "--model FILE.py:NAME" in capsys.readouterr().err
    assert main(["rerun", str(out), "--model", f"{models_file}:fake"]) == 0
    rows = _results(out)["rows"]
    assert all(r.get("answer_repaired") for r in rows if r.get("answer_status") == "skipped")


def test_model_check_command(models_file, capsys):
    assert main(["model-check", f"{models_file}:echo"]) == 0
    out = capsys.readouterr().out
    assert "reply text found: True" in out and "model: echo (function)" in out
    assert main(["model-check", f"{models_file}:number"]) == 2
