import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("numpy")
hb = pytest.importorskip("tisma_code.hope_bridge")


@pytest.mark.skipif(not shutil.which("bwrap"), reason="sin bubblewrap")
def test_hope_python_is_jailed(tmp_path):
    h = hb.HopeTools(tmp_path / "d", chat=None)
    assert h.run("python", "print(6*7)").strip().endswith("42")
    secret = Path.home() / ".tisma_test_secret"
    secret.write_text("no deberías leer esto")
    try:
        out = h.run("python", f"print(open({str(secret)!r}).read())")
        assert "no deberías" not in out
        net = h.run("python", "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3); print('RED')")
        assert "RED" not in net
        w = h.run("python", "open('/usr/x','w')")
        assert "error" in w.lower() or "read-only" in w.lower() or "permission" in w.lower()
    finally:
        secret.unlink()
