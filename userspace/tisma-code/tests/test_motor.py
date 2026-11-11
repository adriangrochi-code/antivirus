"""Motor: ventana real (Strata /health, llama-server /props) y compatibilidad de Strata con el equipo."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import models  # noqa: E402


def serve(routes):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = routes.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            if body is not None:
                self.wfile.write(json.dumps(body).encode())

        def log_message(self, *a):
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_server_context_strata_and_llama():
    a = serve({"/health": {"status": "ok", "max_context": 65536, "loaded": True}})
    b = serve({"/health": {"status": "ok"}, "/props": {"default_generation_settings": {"n_ctx": 16384}}})
    try:
        assert models.server_context(f"http://127.0.0.1:{a.server_port}/v1") == 65536
        assert models.server_context(f"http://127.0.0.1:{b.server_port}/v1/") == 16384
        assert models.server_context("http://127.0.0.1:9/v1") is None           # motor apagado
    finally:
        a.shutdown()
        b.shutdown()


def test_strata_status_thinkpad_vs_desktop():
    a275 = {"ram_mb": 15600, "gpus": [], "vram_mb": 0}
    assert not models.strata_status(a275)["ok"] and "llama.cpp" in models.strata_status(a275)["motivo"]
    small = {"ram_mb": 16000, "gpus": [{"name": "RTX 3060", "vram_mb": 12288}], "vram_mb": 12288}
    assert not models.strata_status(small)["ok"]
    big = {"ram_mb": 32000, "gpus": [{"name": "RTX 4070", "vram_mb": 12288}], "vram_mb": 12288}
    assert models.strata_status(big)["ok"]
