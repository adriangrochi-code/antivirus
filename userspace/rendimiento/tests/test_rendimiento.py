import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_rendimiento import server  # noqa: E402


@pytest.fixture
def srv(monkeypatch):
    calls = []

    class R:
        returncode, stdout, stderr = 0, "ok", ""
    monkeypatch.setattr(server, "RUN", lambda argv, **k: calls.append(argv) or R())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_port, calls
    httpd.shutdown()


def req(port, method, path, body=None, token=True, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}", "Content-Type": "application/json"}
    if token:
        h["X-Token"] = server.TOKEN
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    return r.status, r.read()


def test_requires_token_and_host(srv):
    port, _ = srv
    assert req(port, "GET", "/api/estado", token=False)[0] == 403
    assert req(port, "GET", "/api/estado", host="evil.example:1")[0] == 403
    assert req(port, "GET", "/api/estado")[0] == 200


def test_actions_validated_before_running(srv):
    port, calls = srv
    for body in ({"gpu": 40}, {"gpu": -1}, {"gpu": True}, {"gpu": "5; rm -rf /"}, {"turbo": "si"}, {}):
        assert req(port, "POST", "/api/oc", body)[0] == 400
    assert req(port, "POST", "/api/perfil", {"perfil": "turbo"})[0] == 400
    assert calls == []                                       # nada se ejecutó
    assert req(port, "POST", "/api/oc", {"gpu": 10, "turbo": True})[0] == 200
    assert req(port, "POST", "/api/perfil", {"perfil": "rendimiento"})[0] == 200
    assert calls == [["pkexec", "/usr/bin/centinela", "oc", "--gpu", "10", "--turbo", "si"],
                     ["systemctl", "start", "centinela-perfil@rendimiento.service"]]
