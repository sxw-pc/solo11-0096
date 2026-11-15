import json

from dimcheck.cli import run_once
from dimcheck.contract import load_contract
from dimcheck.report import build_report, render_html
from dimcheck.sources import load_dataset
from dimcheck.util import finding

FACT_HEADER = ["sale_id", "customer_key", "product_key", "date_key", "amount",
               "quantity", "avg_price", "on_hand"]


def test_report_artifacts_and_deterministic_shape(project, tmp_path):
    report, paths = run_once(project.contract, tmp_path / "out", quiet=True)
    assert set(paths) == {"json", "markdown", "html", "run"}
    parsed = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert parsed["verdict"] == "pass"
    assert parsed["exit_code"] == 0
    assert parsed["generated_epoch"] == 1767225600
    assert parsed["summary"]["rows"] == 12
    assert "profiles" in parsed["dataset"]["tables"]["fct_sales"]
    text = paths["markdown"].read_text(encoding="utf-8")
    assert "聚合安全矩阵" in text
    assert "总线矩阵" in text
    assert "dim_customer" in text
    html_text = paths["html"].read_text(encoding="utf-8")
    assert "<script" not in html_text.lower()
    assert "http://" not in html_text
    assert "https://" not in html_text
    assert "Content-Security-Policy" not in html_text
    assert str(project.root) not in paths["json"].read_text(encoding="utf-8")


def test_warning_report_contains_finding_evidence(project, tmp_path):
    project.write_csv(
        "fct_sales.csv", FACT_HEADER,
        [[1001, 1, 1, 1, 10.0, 2, 5.0, 100], [1002, 2, 2, 2, 20.0, 4, 5.0, 120],
         [1003, 1, 3, 3, 7.5, 3, 2.5, 90], [1004, 1, 1, 1, "", 1, 3.0, 80]],
    )
    report, paths = run_once(project.contract, tmp_path / "out", quiet=True)
    assert report["verdict"] == "warn"
    assert report["exit_code"] == 3
    assert report["findings"][0]["code"] == "DIM-MEA-006"
    markdown = paths["markdown"].read_text(encoding="utf-8")
    assert "DIM-MEA-006" in markdown
    assert "可加度量包含空值" in markdown
    html_text = paths["html"].read_text(encoding="utf-8")
    assert "DIM-MEA-006" in html_text
    assert "证据" in html_text


def test_html_escapes_untrusted_text(project):
    contract = load_contract(project.contract)
    dataset = load_dataset(contract)
    injected = finding("DIM-INTERNAL-001", "test", "<object>",
                       "<script>alert('x')</script>")
    report = build_report(contract, dataset, [injected], {}, contract.run_epoch)
    content = render_html(report)
    assert "<script>alert" not in content
    assert "&lt;script&gt;" in content
    assert "<object>" not in content
