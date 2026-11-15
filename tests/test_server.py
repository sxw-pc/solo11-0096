import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from dimcheck.cli import run_once
from dimcheck.server import make_server


def _start(project, tmp_path):
    out = tmp_path / "out"
    run_once(project.contract, out, quiet=True)
    server = make_server(out, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_read_only_console_routes(project, tmp_path):
    server, thread = _start(project, tmp_path)
    host, port = server.server_address[:2]
    assert host == "127.0.0.1"
    base = f"http://127.0.0.1:{port}"
    try:
        with urlopen(base + "/", timeout=5) as response:
            index = response.read().decode("utf-8")
            assert response.status == 200
            assert "最近运行台账" in index
            assert "Content-Security-Policy" in response.headers
        with urlopen(base + "/report.html", timeout=5) as response:
            assert "维模体检台报告" in response.read().decode("utf-8")
        with urlopen(base + "/ledger.json", timeout=5) as response:
            ledger = json.loads(response.read().decode("utf-8"))
            assert len(ledger["runs"]) == 1
        with pytest.raises(HTTPError) as missing:
            urlopen(base + "/missing", timeout=5)
        assert missing.value.code == 404
        with pytest.raises(HTTPError) as traversal:
            urlopen(base + "/%2e%2e/report.json", timeout=5)
        assert traversal.value.code == 404
        request = Request(base + "/", method="POST")
        with pytest.raises(HTTPError) as post:
            urlopen(request, timeout=5)
        assert post.value.code == 405
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_invalid_port_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        make_server(tmp_path, 70000)
