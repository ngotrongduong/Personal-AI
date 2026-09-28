from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json

from imitation.demo_bank import DemoBank
from imitation.evaluate import evaluate
from imitation.policy import PolicyConfig
from scripts import imitation as imitation_cli
from tests.test_imitation_demo_bank import make_frame, write_session


def _bank(tmp_path):
    frame = make_frame()
    first = write_session(tmp_path, "one", frame)
    second = write_session(tmp_path, "two", frame)
    return DemoBank.build([first, second]), first


def test_loso_and_loco_counts_on_synthetic_sessions(tmp_path) -> None:
    bank, _ = _bank(tmp_path)
    config = PolicyConfig(k=2, screen_threshold=0.8, patch_threshold=0.7)

    for mode in ("loso", "loco"):
        report = evaluate(bank, config, mode=mode)
        assert (report.total, report.hits, report.misses, report.abstains) == (2, 2, 0, 0)
        assert report.precision == 1.0
        assert report.coverage == 1.0
        assert report.to_dict()["mode"] == mode


def _run(*args: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = imitation_cli.main(list(args))
    return code, stdout.getvalue(), stderr.getvalue()


def test_cli_bank_eval_and_atomic_output(tmp_path) -> None:
    _bank(tmp_path)
    code, stdout, stderr = _run("bank", str(tmp_path), "--window", "Merchant")
    assert (code, stderr) == (0, "")
    assert "2 demo clicks from 2 session" in stdout

    out = tmp_path.parent / "report.json"
    code, stdout, stderr = _run(
        "eval",
        str(tmp_path),
        "--window",
        "Merchant",
        "--mode",
        "loco",
        "--out",
        str(out),
    )
    assert (code, stderr) == (0, "")
    assert "Precision 100.0%" in stdout
    assert json.loads(out.read_text(encoding="utf-8"))["hits"] == 2
    assert not out.with_name(out.name + ".tmp").exists()


def test_cli_output_guards(tmp_path) -> None:
    _, session = _bank(tmp_path)
    protected = session / "report.json"
    code, _, stderr = _run(
        "eval", str(tmp_path), "--window", "Merchant", "--out", str(protected)
    )
    assert code == 2
    assert "inside recording session" in stderr
    assert not protected.exists()

    out = tmp_path.parent / "existing-report.json"
    out.write_text("keep", encoding="utf-8")
    code, _, stderr = _run(
        "eval", str(tmp_path), "--window", "Merchant", "--out", str(out)
    )
    assert code == 2
    assert "already exists" in stderr
    assert out.read_text(encoding="utf-8") == "keep"
