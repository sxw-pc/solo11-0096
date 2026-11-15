import shutil
from pathlib import Path

import pytest

import dimcheck.cli as cli_module
from dimcheck.cli import main
from dimcheck.util import (PathError, ensure_out_dir, guard_contract_path,
                           is_network_path)

FACT_HEADER = ["sale_id", "customer_key", "product_key", "date_key", "amount",
               "quantity", "avg_price", "on_hand"]


def test_clean_run_returns_zero(project, tmp_path, capsys):
    code = main(["run", "--contract", str(project.contract),
                 "--out", str(tmp_path / "out")])
    assert code == 0
    assert (tmp_path / "out" / "report.json").is_file()
    assert (tmp_path / "out" / "ledger.sqlite3").is_file()
    assert "verdict=pass" in capsys.readouterr().out


def test_warning_run_returns_three(project, tmp_path):
    project.write_csv(
        "fct_sales.csv", FACT_HEADER,
        [[1001, 1, 1, 1, 10.0, 2, 5.0, 100], [1002, 2, 2, 2, "", 4, 5.0, 120],
         [1003, 1, 3, 3, 7.5, 3, 2.5, 90]],
    )
    assert main(["run", "--contract", str(project.contract),
                 "--out", str(tmp_path / "out"), "--quiet"]) == 3


def test_error_run_returns_two(project, tmp_path):
    project.write_csv(
        "dim_customer.csv", ["customer_key", "customer_id", "segment"],
        [[1, "C001", "retail"], [2, "C002", "corp"], [2, "C003", "public"]],
    )
    assert main(["run", "--contract", str(project.contract),
                 "--out", str(tmp_path / "out"), "--quiet"]) == 2


def test_invalid_contract_returns_one(project, tmp_path, capsys):
    project.model["schema_version"] = 9
    project.save()
    assert main(["run", "--contract", str(project.contract),
                 "--out", str(tmp_path / "out"), "--quiet"]) == 1
    assert "DIM-CONTRACT-004" in capsys.readouterr().err


def test_row_limit_returns_one(project, tmp_path, capsys):
    project.model["dataset"]["max_rows"] = 2
    project.save()
    assert main(["run", "--contract", str(project.contract),
                 "--out", str(tmp_path / "out"), "--quiet"]) == 1
    assert "DIM-LIM-001" in capsys.readouterr().err


def test_source_traversal_returns_one(project, tmp_path, capsys):
    (tmp_path / "secret.csv").write_text("x\n1\n", encoding="utf-8")
    project.model["dimensions"][0]["source"] = "../secret.csv"
    project.save()
    assert main(["run", "--contract", str(project.contract),
                 "--out", str(tmp_path / "out"), "--quiet"]) == 1
    assert "DIM-PATH-001" in capsys.readouterr().err


def test_sensitive_and_network_paths_are_rejected():
    assert is_network_path("//server/share")
    for raw in ("//server/share", "/etc"):
        with pytest.raises(PathError):
            ensure_out_dir(raw)
        with pytest.raises(PathError):
            guard_contract_path(raw)


def test_explain_known_and_unknown(capsys):
    assert main(["explain", "DIM-MEA-006"]) == 0
    output = capsys.readouterr().out
    assert "DIM-MEA-006" in output
    assert "修复" in output
    assert main(["explain", "DIM-NOPE"]) == 1
    assert "DIM-EXPLAIN-001" in capsys.readouterr().err
    assert main(["explain"]) == 0
    assert "DIM-CONTRACT-001" in capsys.readouterr().out


def test_bad_arguments_use_exit_one(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--not-a-real-option"])
    assert exc.value.code == 1
    assert "DIM-CLI-001" in capsys.readouterr().err


def test_demo_runs_from_clean_temp_repo(project, tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    (repo / "contracts").mkdir(parents=True)
    (repo / "scripts").mkdir()
    shutil.copy(root / "contracts" / "model.yaml", repo / "contracts" / "model.yaml")
    shutil.copy(root / "scripts" / "make_fixtures.py", repo / "scripts" / "make_fixtures.py")
    monkeypatch.setattr(cli_module, "repo_root", lambda: repo)
    code = main(["demo", "--out", str(tmp_path / "out")])
    assert code == 0
    assert (repo / "fixtures" / "fct_ridership.csv").is_file()
    assert (tmp_path / "out" / "report.html").is_file()
    assert "verdict=pass" in capsys.readouterr().out
