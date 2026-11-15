import json

from dimcheck.cli import run_once


def test_reports_are_byte_stable_except_run_timing(project, tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    _, first = run_once(project.contract, tmp_path / "out1", quiet=True)
    _, second = run_once(project.contract, tmp_path / "out2", quiet=True)
    for key in ("json", "markdown", "html"):
        assert first[key].read_bytes() == second[key].read_bytes(), key
    first_run = json.loads(first["run"].read_text(encoding="utf-8"))
    second_run = json.loads(second["run"].read_text(encoding="utf-8"))
    assert first_run.pop("started_at")
    assert second_run.pop("started_at")
    first_run.pop("duration_ms")
    second_run.pop("duration_ms")
    assert first_run == second_run


def test_source_date_epoch_controls_report_time(project, tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    report, _ = run_once(project.contract, tmp_path / "out", quiet=True)
    assert report["generated_epoch"] == 1700000000
    assert report["generated_at"] == "2023-11-14T22:13:20Z"


def test_invalid_source_date_epoch_falls_back(project, tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-a-number")
    report, _ = run_once(project.contract, tmp_path / "out", quiet=True)
    assert report["generated_epoch"] == 1767225600
