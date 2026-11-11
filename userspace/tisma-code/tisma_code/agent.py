"""Bucle del agente: modelos locales (API compatible con OpenAI) + herramientas + arnés.

Ciclo de una tarea (inspirado en el harness F9 de Hope):
1. Contexto: memoria del proyecto (TISMA.md) y experiencia previa (soluciones y lecciones).
2. El modelo elegido por el enrutador (UCB1) trabaja con las herramientas; cada escritura o
   comando requiere aprobación.
3. Al terminar, si cambió archivos, ``verify`` corre automáticamente (Loop Controller):
   si falla, la salida vuelve como crítica, se escala a un modelo más potente si lo hay y se
   reintenta; se detiene al verificar, si no hay progreso o al agotar el presupuesto.
4. Con velocidad objetivo, el tiempo sobrante se usa para una autorrevisión del diff.
5. Registro (PESAM), aprendizaje (solución/lección) y episodio de entrenamiento si quedó verificado.
"""

from __future__ import annotations

import difflib
import json
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid

from . import harness, memory, review, tools

try:                                     # Hope es opcional: sin vendor/ el agente funciona igual
    from .hope_bridge import HopeTools, tool_help
except Exception:  # noqa: BLE001
    HopeTools = None

SYSTEM_PROMPT = """Eres TISMA Code, un agente de programación que trabaja en la carpeta del proyecto \
del usuario en su propio equipo. Respondes en el idioma del usuario.

Cómo trabajas:
1. Para tareas de varios pasos, primero crea un plan con update_todos y mantenlo al día.
2. Antes de cambiar código, explora: list_dir, search y read_file. No inventes el contenido de archivos.
3. Para modificar, usa edit_file con un fragmento copiado de read_file; write_file solo para archivos nuevos.
4. Después de cambiar código, llama a verify (tests/compilación) y corrige lo que falle.
4b. Para cálculos, unidades o problemas de ingeniería/ciencia usa la herramienta hope (exacta) o consejo
    (varios agentes + verificación); nunca calcules de memoria. Escribe los cálculos como «Paso n: … = …»
    y termina con «Resultado: …»: un crítico revisa cada paso.
5. El usuario aprueba cada escritura y cada comando. Si deniega, no insistas: pregunta o propone otra vía.
6. Los comandos corren aislados, sin red y sin acceso fuera del proyecto.
7. Sé conciso. Al terminar, resume qué cambiaste y cómo comprobarlo.

Para usar una herramienta, usa la llamada de función del sistema. Si no puedes, escribe exactamente:
<tool_call>{"name": "read_file", "arguments": {"path": "main.py"}}</tool_call>"""

MAX_STEPS = 50


class LLMError(Exception):
    pass


def chat_completion(endpoint: str, model: str, messages: list, tool_schemas: list | None,
                    api_key: str = "local", timeout: int = 900) -> tuple[dict, dict]:
    body = {"model": model, "messages": messages, "temperature": 0.2, "top_p": 0.9}
    if tool_schemas:
        body["tools"] = tool_schemas
        body["tool_choice"] = "auto"
    req = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (localhost)
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise LLMError(f"El motor respondió {e.code}: {e.read()[:500].decode(errors='replace')}")
    except (urllib.error.URLError, OSError) as e:
        raise LLMError(f"No se pudo conectar con el motor en {endpoint}: {e}")
    try:
        return data["choices"][0]["message"], data.get("usage") or {}
    except (KeyError, IndexError):
        raise LLMError(f"Respuesta inesperada del motor: {str(data)[:300]}")


class Session:
    def __init__(self, workspace: str, endpoint: str, model: str, llm=chat_completion,
                 context_tokens: int = 32768, models: list[review.ModelSpec] | None = None,
                 target_tps: float = 0.0, auto_verify: bool = True, kb_dir=None):
        self.ws = tools.Workspace(workspace)
        self.llm = llm
        self.context_tokens = context_tokens
        self.target_tps = target_tps
        self.auto_verify = auto_verify
        self.kb = memory.KnowledgeBase(self.ws.root, **({"base": kb_dir} if kb_dir else {}))
        specs = models or [review.ModelSpec("principal", endpoint, model)]
        self.router = review.Router(specs, self.kb.model_stats())
        self.current = self.router.first()
        # El prefijo (sistema + historial) nunca se reescribe salvo al compactar:
        # así el motor reutiliza su caché KV y cada paso solo procesa lo nuevo.
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT +
                                      f"\n\nCarpeta del proyecto: {self.ws.root}" +
                                      self._checks_hint() + harness.project_memory(self.ws.root)}]
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.always_allow: set[str] = set()
        self.todos: list[dict] = []
        self.stats = {"steps": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0,
                      "repaired_calls": 0, "fuzzy_edits": 0, "compactions": 0,
                      "verified_tasks": 0, "review_rounds": 0, "escalations": 0}
        self._loops = harness.LoopDetector()
        self._pending: dict[str, tuple[threading.Event, dict]] = {}
        self._busy = threading.Lock()
        self._cancel = threading.Event()
        self._originals: dict[str, str] = {}     # archivos tocados en la tarea → contenido previo
        self.hope = None
        if HopeTools is not None:
            try:
                self.hope = HopeTools(self.kb.dir / "hope", chat=self._plain_chat,
                                      endpoint=self.current.endpoint, model=self.current.model)
            except Exception as e:  # noqa: BLE001
                self.emit("info", message=f"Herramientas de Hope no disponibles: {e}")

    def _plain_chat(self, messages: list, temperature: float = 0.2) -> str:
        msg, usage = self.llm(self.current.endpoint, self.current.model, messages, None)
        self.stats["prompt_tokens"] += usage.get("prompt_tokens", 0)
        self.stats["completion_tokens"] += usage.get("completion_tokens", 0)
        return msg.get("content") or ""

    # Compatibilidad: endpoint/modelo del modelo actual
    @property
    def endpoint(self) -> str:
        return self.current.endpoint

    @endpoint.setter
    def endpoint(self, v: str) -> None:
        self.current.endpoint = v

    @property
    def model(self) -> str:
        return self.current.model

    @model.setter
    def model(self, v: str) -> None:
        self.current.model = v

    def _checks_hint(self) -> str:
        checks = harness.detect_checks(self.ws.root)
        return ("\nComprobaciones detectadas para verify: " + "; ".join(checks)) if checks else ""

    # ------------------------------------------------------------ eventos / aprobaciones
    def emit(self, kind: str, **data) -> None:
        self.events.put({"type": kind, **data})

    def resolve_approval(self, approval_id: str, allow: bool, always: bool = False) -> bool:
        item = self._pending.get(approval_id)
        if not item:
            return False
        ev, info = item
        info["allow"] = allow
        if allow and always:
            self.always_allow.add(info["tool"])
        ev.set()
        return True

    def _ask(self, tool: str, args: dict, preview: str) -> bool:
        if tool not in tools.NEEDS_APPROVAL or tool in self.always_allow:
            return True
        aid = uuid.uuid4().hex[:12]
        ev = threading.Event()
        info: dict = {"tool": tool, "allow": False}
        self._pending[aid] = (ev, info)
        self.emit("approval", id=aid, tool=tool, args=args, preview=preview)
        while not ev.wait(0.5):
            if self._cancel.is_set():
                break
        self._pending.pop(aid, None)
        return bool(info["allow"])

    def cancel(self) -> None:
        self._cancel.set()

    # ------------------------------------------------------------ tarea
    def send(self, text: str) -> None:
        if not self._busy.acquire(blocking=False):
            self.emit("error", message="El agente sigue trabajando; espera o detenlo.")
            return
        self._cancel.clear()
        self._originals.clear()
        try:
            self._task(text)
        except LLMError as e:
            self.emit("error", message=str(e))
            self.kb.log({"event": "error", "task": text, "error": str(e)})
        finally:
            self._busy.release()
            self.emit("idle", stats=self.stats)

    def _task(self, text: str) -> None:
        experience, used_ids = self.kb.context(text)
        start_msgs = len(self.messages)
        self.messages.append({"role": "user", "content": text + (f"\n\n{experience}" if experience else "")})
        if experience:
            self.emit("info", message=f"Usando {len(used_ids)} recuerdo(s) de tareas parecidas")
        self.current = self.router.first()
        loop = review.ReviewLoop()
        t0, tokens0 = time.monotonic(), self.stats["completion_tokens"]
        verified, last_verify, self_reviewed = None, "", False

        crit_rounds = 0
        while True:
            final = self._agent_loop()
            if self._cancel.is_set():
                break
            if not self._originals:
                # Respuesta sin cambios de código: si trae cálculos, el crítico de Hope los revisa
                if self.hope and "Resultado:" in final and crit_rounds < 2:
                    c = self.hope.critique(text, final)
                    self.emit("verify", ok=c["verdict"] == "ok", output=c["feedback"] or "Cálculos verificados",
                              state=c["verdict"])
                    if c["verdict"] in ("errores", "debe_declarar_limitacion"):
                        crit_rounds += 1
                        self.messages.append({"role": "user", "content": "El crítico encontró problemas en tus "
                                              "cálculos. Corrígelos usando la herramienta hope:\n" + c["feedback"]})
                        continue
                    verified = c["verdict"] == "ok"
                break                                  # sin cambios: nada más que verificar
            checks = harness.detect_checks(self.ws.root)
            if not (self.auto_verify and checks):
                verified = None
            else:
                self.emit("info", message="Verificando los cambios (pruebas/compilación)…")
                last_verify = tools.execute(self.ws, "verify", {})
                state = loop.next(last_verify)
                self.emit("verify", ok=state == "verificado", output=tools._clip(last_verify, 4000), state=state)
                if state == "verificado":
                    verified = True
                else:
                    verified = False
                    if state != "reintentar":
                        self.emit("info", message="Sin progreso en la corrección: se detiene y te deja el último resultado"
                                  if state == "sin_progreso" else "Se agotaron las rondas de corrección")
                        break
                    self.stats["review_rounds"] += 1
                    nxt = self.router.escalate(self.current)
                    if nxt and loop.rounds >= 2:       # segunda falla: modelo más potente
                        self.kb.record_model(self.current.name, False)
                        self.current = nxt
                        self.stats["escalations"] += 1
                        self.emit("info", message=f"Escalando al modelo {nxt.name}")
                    self.messages.append({"role": "user", "content": review.critique_message(
                        last_verify, loop.rounds, nxt.name if nxt and self.current is nxt else None)})
                    continue
            # Anytime: tiempo sobrante → autorrevisión del diff (una vez por tarea)
            spare = review.anytime_budget(self.stats["completion_tokens"] - tokens0,
                                          time.monotonic() - t0, self.target_tps)
            if spare > 5 and not self_reviewed:
                self_reviewed = True
                self.emit("info", message=f"Sobran {spare:.0f} s: autorrevisión del diff")
                self.messages.append({"role": "user", "content": review.SELF_REVIEW.format(diff=self.diff()[:12000])})
                before = len(self._originals), self.diff()
                final = self._agent_loop()
                if self.diff() != before[1] and checks:
                    continue                           # cambió algo: volver a verificar
            break

        self._learn(text, final if 'final' in locals() else "", verified, loop, used_ids, start_msgs)

    def _learn(self, task: str, summary: str, verified, loop: review.ReviewLoop,
               used_ids: list[int], start: int) -> None:
        files = sorted(self._originals)
        if files:
            self.kb.learn_solution(task, summary or "", files, bool(verified))
            if verified and loop.failures:
                self.kb.learn_lesson(task, loop.failures[-1], summary or self.diff()[:800])
        if used_ids:
            self.kb.feedback(used_ids, bool(verified))
        if verified is not None:
            self.kb.record_model(self.current.name, bool(verified))
        if verified:
            self.stats["verified_tasks"] += 1
            self.kb.save_episode(task, self.messages[:1] + self.messages[start:], self.current.model)
        self.kb.log({"event": "task", "task": task, "files": files, "verified": verified,
                     "model": self.current.name, "rounds": loop.rounds})
        self.emit("task_done", verified=verified, files=files, model=self.current.name)

    def diff(self) -> str:
        out = []
        for rel, old in sorted(self._originals.items()):
            p = self.ws.root / rel
            new = p.read_text(errors="replace") if p.exists() else ""
            out.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}"))
        return "".join(out)

    # ------------------------------------------------------------ modelo
    def _call_llm(self, messages: list, schemas: list | None) -> dict:
        t0 = time.monotonic()
        msg, usage = self.llm(self.current.endpoint, self.current.model, messages, schemas)
        dt = time.monotonic() - t0
        self.stats["seconds"] += dt
        self.stats["prompt_tokens"] += usage.get("prompt_tokens", 0)
        self.stats["completion_tokens"] += usage.get("completion_tokens", 0)
        tps = usage.get("completion_tokens", 0) / dt if dt > 0 else 0
        self.emit("metrics", seconds=round(dt, 1), tokens_s=round(tps, 1), usage=usage, model=self.current.name,
                  context=harness.estimate_tokens(self.messages), context_max=self.context_tokens)
        return msg

    def _maybe_compact(self) -> None:
        if harness.estimate_tokens(self.messages) < self.context_tokens * 0.75 or len(self.messages) < 8:
            return
        keep = 6
        middle = self.messages[1:-keep]
        while middle and self.messages[len(middle) + 1]["role"] == "tool":
            middle = middle[:-1]               # no partir un par llamada/resultado
        if not middle:
            return
        self.emit("info", message="Contexto casi lleno: resumiendo la conversación anterior…")
        summary = self._call_llm(harness.compaction_prompt(middle), None).get("content") or ""
        self.messages = ([self.messages[0],
                          {"role": "user", "content": "Resumen de lo hecho hasta ahora:\n" + summary},
                          {"role": "assistant", "content": "Entendido, continúo desde ahí."}]
                         + self.messages[len(middle) + 1:])
        self.stats["compactions"] += 1

    def _agent_loop(self) -> str:
        """Pasos del modelo hasta que responde sin herramientas. Devuelve su respuesta final."""
        for _ in range(MAX_STEPS):
            if self._cancel.is_set():
                self.emit("info", message="Detenido por el usuario")
                return ""
            self._maybe_compact()
            self.emit("thinking", model=self.current.name)
            msg = self._call_llm(self.messages, tools.TOOL_SCHEMAS)
            self.stats["steps"] += 1
            content = msg.get("content") or ""
            calls = msg.get("tool_calls") or []
            if not calls and content:
                calls = harness.extract_text_tool_calls(content, tools.TOOL_NAMES)
                if calls:
                    self.stats["repaired_calls"] += len(calls)
                    content = harness._TOOL_TAG.sub("", content).strip()
            assistant = {"role": "assistant", "content": content}
            if calls:
                assistant["tool_calls"] = calls
            self.messages.append(assistant)
            if content:
                self.emit("assistant", text=content)
            if not calls:
                return content
            for call in calls:
                self.messages.append(self._run_call(call))
        self.emit("info", message=f"Se alcanzó el límite de {MAX_STEPS} pasos")
        return ""

    def _run_call(self, call: dict) -> dict:
        fn = call.get("function", {})
        name = fn.get("name", "")
        cid = call.get("id") or uuid.uuid4().hex[:12]

        def reply(result: str, ok: bool, args: dict | None = None) -> dict:
            self.emit("tool", id=cid, tool=name, args=args or {}, result=tools._clip(result, 4000), ok=ok)
            return {"role": "tool", "tool_call_id": cid, "content": result}

        if name not in tools.TOOL_NAMES:
            return reply(f"Error: la herramienta '{name}' no existe. Disponibles: "
                         f"{', '.join(sorted(tools.TOOL_NAMES))}.", False)
        args, err = harness.normalize_arguments(fn.get("arguments"))
        if err:
            return reply("Error: " + err, False)
        if self._loops.check(name, args):
            return reply("Aviso: es la tercera vez seguida que haces exactamente esta llamada. "
                         "Cambia de enfoque: lee otro archivo, revisa el error o pregunta al usuario.", False, args)
        if name in ("hope", "hope_ayuda", "consejo"):
            if not self.hope:
                return reply("Error: las herramientas de Hope no están instaladas.", False, args)
            try:
                if name == "hope":
                    out = self.hope.run(str(args.get("nombre", "")), str(args.get("entrada", "")))
                    return reply(out, not out.startswith(("error", "denegado")), args)
                if name == "hope_ayuda":
                    return reply(tool_help(str(args.get("tema", ""))), True, args)
                self.emit("info", message="Consultando al consejo de Hope (4 agentes + verificador)…")
                r = self.hope.council(str(args["problema"]), str(args.get("tema") or "general"))
                txt = (f"Respuesta: {r['answer']} (veredicto: {r['verdict']}, agente: {r['chosen_agent']})\n"
                       f"Razonamiento elegido:\n{r['reasoning']}\nRanking: " +
                       "; ".join(f"{x['agent']}={x['verdict']}:{x['answer']}" for x in r["ranking"]))
                return reply(txt, r["verdict"] == "ok", args)
            except Exception as e:  # noqa: BLE001
                return reply(f"Error en Hope: {e}", False, args)
        if name == "update_todos":
            self.todos = [t for t in args.get("todos", []) if isinstance(t, dict)]
            self.emit("todos", todos=self.todos)
            return reply("Lista de tareas actualizada.", True, args)
        try:
            pv = tools.preview(self.ws, name, args)
            target = None
            if name in ("write_file", "edit_file"):
                target = self.ws.resolve(args["path"])
                if name == "edit_file" and target.is_file() and args.get("old") not in target.read_text(errors="replace"):
                    self.stats["fuzzy_edits"] += 1
            if not self._ask(name, args, pv):
                return reply("El usuario denegó esta acción.", False, args)
            if target is not None:
                rel = self.ws.rel(target)
                if rel not in self._originals:
                    self._originals[rel] = target.read_text(errors="replace") if target.is_file() else ""
            result = tools.execute(self.ws, name, args)
            return reply(result, True, args)
        except (tools.ToolError, KeyError, OSError, ValueError) as e:
            return reply(f"Error: {e}", False, args)
