"""Cliente MCP mínimo (transporte stdio, JSON-RPC 2.0 por líneas).

Se usa para REA (Reverse Engineer Anything, MIT). El servidor MCP se lanza dentro de bubblewrap
(sin red, $HOME oculto, solo el proyecto en lectura y una carpeta de trabajo propia).
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from typing import Any


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, argv: list[str], env: dict | None = None, cwd: str | None = None, timeout: float = 120):
        self.timeout = timeout
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1, env=env, cwd=cwd)
        self._id = 0
        self._lock = threading.Lock()
        self._waiting: dict[int, queue.Queue] = {}
        threading.Thread(target=self._reader, daemon=True).start()
        self.call_raw("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "tisma-code", "version": "0.1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _reader(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue                       # ruido en stdout: se ignora
            q = self._waiting.get(msg.get("id")) if isinstance(msg, dict) else None
            if q:
                q.put(msg)
        for q in list(self._waiting.values()):
            q.put({"error": {"message": "el servidor MCP terminó"}})

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def call_raw(self, method: str, params: dict | None = None) -> Any:
        with self._lock:
            self._id += 1
            rid = self._id
            q: queue.Queue = queue.Queue()
            self._waiting[rid] = q
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        try:
            msg = q.get(timeout=self.timeout)
        except queue.Empty:
            raise MCPError(f"Tiempo agotado esperando {method}")
        finally:
            self._waiting.pop(rid, None)
        if "error" in msg:
            raise MCPError(str(msg["error"].get("message", msg["error"]))[:500])
        return msg.get("result")

    def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        while True:
            r = self.call_raw("tools/list", {"cursor": cursor} if cursor else {})
            tools += r.get("tools", [])
            cursor = r.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict) -> str:
        r = self.call_raw("tools/call", {"name": name, "arguments": arguments})
        parts = []
        for c in r.get("content", []):
            if c.get("type") == "text":
                parts.append(c["text"])
            else:
                parts.append(f"[{c.get('type')}]")
        if r.get("structuredContent") and not parts:
            parts.append(json.dumps(r["structuredContent"], ensure_ascii=False)[:20000])
        text = "\n".join(parts)
        return ("Error: " + text) if r.get("isError") else text

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
