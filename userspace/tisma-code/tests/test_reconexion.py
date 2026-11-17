"""El motor local se reinicia justo al primer pedido (la A275: «Remote end closed connection»): se reintenta."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from tisma_code import agent


def test_corte_y_503_se_reintentan(monkeypatch):
    monkeypatch.setattr(agent, "_check_local_owner", lambda e: None)
    monkeypatch.setattr(agent, "modo_ia", lambda: None)
    pedidos = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            pedidos.append(1)
            self.rfile.read(int(self.headers["Content-Length"]))
            if len(pedidos) == 1:                       # se corta sin responder (motor que se cae)
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if len(pedidos) == 2:                       # cargando el modelo
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"error":"Loading model"}')
                return
            cuerpo = json.dumps({"choices": [{"message": {"role": "assistant", "content": "hola"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        msg, _ = agent.chat_completion(f"http://127.0.0.1:{srv.server_port}/v1", "m", [], None)
    finally:
        srv.shutdown()
    assert msg["content"] == "hola" and len(pedidos) == 3
