import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_project_line_budget_and_single_file_limit():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_limits.py"), "--root", str(ROOT)],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
    assert "total=" in result.stdout


def test_limit_checker_detects_oversized_file(tmp_path):
    (tmp_path / "big.py").write_text("x = 1\n" * 301, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_limits.py"),
         "--root", str(tmp_path), "--min", "0", "--file-max", "300"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    assert "big.py: 301" in result.stdout
