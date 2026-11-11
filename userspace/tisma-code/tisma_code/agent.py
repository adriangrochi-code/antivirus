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
from . import devtools
from .docs import DocSpace

try:                                     # Hope es opcional: sin vendor/ el agente funciona igual
    from .hope_bridge import HopeTools, tool_help
except Exception:  # noqa: BLE001
    HopeTools = None

SYSTEM_PROMPT = """Eres TISMA Code, un agente de programación que trabaja en la carpeta del proyecto \
del usuario en su propio equipo. Respondes en el idioma del usuario.

Cómo trabajas:
1. Para tareas de varios pasos, primero crea un plan con update_todos y mantenlo al día.
2. Antes de cambiar código, explora: mapa_repo, simbolo, search y read_file. No inventes el contenido de archivos.
   Usa git (status, diff, log, blame) para entender el historial.
3. Para modificar, usa edit_file con un fragmento copiado de read_file; write_file solo para archivos nuevos.
4. Tras cada edición recibirás los errores de los linters en las líneas que cambiaste: corrígelos antes de seguir.
   Al terminar, llama a verify (tests/compilación) y corrige lo que falle. Usa diagnosticos(completo=true)
   para revisar tipos y seguridad antes de dar el trabajo por terminado.
4b. Para cálculos, unidades o problemas de ingeniería/ciencia usa la herramienta hope (exacta) o consejo
    (varios agentes + verificación); nunca calcules de memoria. Escribe los cálculos como «Paso n: … = …»
    y termina con «Resultado: …»: un crítico revisa cada paso.
4d. Para entender o desmontar cualquier programa sin su código (binarios, apps JavaScript/Electron, APK,
    firmware, protocolos, webs) usa REA: primero rea_guia para el procedimiento, luego rea_buscar y rea.
    Empieza en modo estático; pide rea_modo (dinámico) solo si la evidencia estática no basta.
    Para dudas de documentación o investigación usa jarvis.
4e. Para investigaciones largas o para recrear/rediseñar algo a partir de una referencia (web, app, juego,
    interfaz) usa Craft Anything: craft_guia para el método, craft_buscar y craft. Lleva la memoria de la
    investigación (brief, plan, hipótesis, hallazgos con ID de evidencia) y cierra el ciclo
    Estudiar → Construir → Verificar → Refinar con una comparación medida contra la referencia.
4c. Antes de investigar desde cero, busca con docs_buscar en los documentos locales (manuales del usuario
    y lo que tú mismo guardaste antes). Cuando descubras algo útil (cómo funciona una parte, la causa de un
    error) guárdalo con docs_guardar para la próxima vez.
5. El usuario aprueba cada escritura y cada comando. Si deniega, no insistas: pregunta o propone otra vía.
6. Los comandos corren aislados, sin red y sin acceso fuera del proyecto.
7. Sé conciso. Al terminar, resume qué cambiaste y cómo comprobarlo.

Para usar una herramienta, usa la llamada de función del sistema. Si no puedes, escribe exactamente:
<tool_call>{"name": "read_file", "arguments": {"path": "main.py"}}</tool_call>""" + harness.RIGOR_PROMPT

MAX_STEPS = 50


class LLMError(Exception):
    pass


_OWNER_OK: dict[int, float] = {}


def _check_local_owner(endpoint: str) -> None:
    from urllib.parse import urlparse

    from .models import port_owner_is_me
    u = urlparse(endpoint)
    if u.hostname not in ("127.0.0.1", "localhost", "::1"):
        return
    port = u.port or 80
    if time.monotonic() - _OWNER_OK.get(port, -1e9) < 30:
        return
    if not port_owner_is_me(port):
        raise LLMError(f"El motor en el puerto {port} no pertenece a tu usuario: por seguridad no se usa "
                       "(otro usuario del equipo podría estar suplantándolo)")
    _OWNER_OK[port] = time.monotonic()


def chat_completion(endpoint: str, model: str, messages: list, tool_schemas: list | None,
                    api_key: str = "local", timeout: int = 900) -> tuple[dict, dict]:
    _check_local_owner(endpoint)
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
                 target_tps: float = 0.0, auto_verify: bool = True, kb_dir=None, especialistas=None):
        self.ws = tools.Workspace(workspace)
        self.llm = llm
        self.events: "queue.Queue[dict]" = queue.Queue()
        # Modelos especialistas (varios modelos a la vez, uno por tarea): ~/.config/tisma-code/especialistas.json
        self.especialistas = especialistas
        if self.especialistas is None:
            try:
                from .especialistas import Pool
                self.especialistas = Pool(llm)
            except Exception as e:  # noqa: BLE001 (configuración no válida: se avisa y se sigue sin ellos)
                self.emit("info", message=f"Especialistas desactivados: {e}")
        self.schemas = list(tools.TOOL_SCHEMAS)
        if self.especialistas and self.especialistas.specs:
            from .especialistas import SCHEMAS as ESP_SCHEMAS
            self.schemas += ESP_SCHEMAS
        self.tool_names = {t["function"]["name"] for t in self.schemas}
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
                                      self._checks_hint() + harness.project_memory(self.ws.root) +
                                      (self.especialistas.describe() if self.especialistas else "")}]
        self.always_allow: set[str] = set()
        self.todos: list[dict] = []
        self._mapped = False
        self._evidence: set[str] = set()
        self.stats = {"rigor_checks": 0, "lint_feedback": 0, "steps": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0,
                      "repaired_calls": 0, "fuzzy_edits": 0, "compactions": 0,
                      "verified_tasks": 0, "review_rounds": 0, "escalations": 0}
        self._loops = harness.LoopDetector()
        self._pending: dict[str, tuple[threading.Event, dict]] = {}
        self._busy = threading.Lock()
        self._cancel = threading.Event()
        self._originals: dict[str, str] = {}     # archivos tocados en la tarea → contenido previo
        self.docs = DocSpace(self.ws.root, self.kb.dir)
        self.rea = None                       # REA se arranca solo si el modelo lo usa
        self.craft = None                     # Craft Anything, igual: bajo demanda
        self.hope = None
        if HopeTools is not None:
            try:
                self.hope = HopeTools(self.kb.dir / "hope", chat=self._role_chat("hope"),
                                      endpoint=self.current.endpoint, model=self.current.model)
            except Exception as e:  # noqa: BLE001
                self.emit("info", message=f"Herramientas de Hope no disponibles: {e}")

    def _role_chat(self, role: str):
        """Cliente de chat para una tarea interna: el especialista asignado a ese rol, o el modelo principal."""
        name = (self.especialistas.roles.get(role) if self.especialistas else None)
        if not name:
            return self._plain_chat
        return lambda messages, temperature=0.2: self.especialistas.chat(name, messages)

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
        if allow and always and info["tool"] == "verify":
            self._trust_file.write_text("El usuario confió en las pruebas de este proyecto\n")
        ev.set()
        return True

    @property
    def _trust_file(self):
        return self.kb.dir / "confianza-verify"

    def trusted(self) -> bool:
        """Las pruebas del repo son código del repo: solo se ejecutan solas si confiaste en el proyecto."""
        return self._trust_file.exists()

    def _ask(self, tool: str, args: dict, preview: str) -> bool:
        if tool == "verify" and self.trusted():
            return True
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
        except Exception as e:  # noqa: BLE001 — un fallo interno nunca debe dejar la sesión colgada
            import traceback
            self.emit("error", message=f"Error interno de TISMA Code: {type(e).__name__}: {e}")
            self.kb.log({"event": "internal_error", "task": text, "trace": traceback.format_exc()})
        finally:
            self._busy.release()
            self.emit("idle", stats=self.stats)

    def _task(self, text: str) -> None:
        experience, used_ids = self.kb.context(text)
        start_msgs = len(self.messages)
        extra = f"\n\n{experience}" if experience else ""
        if not self._mapped:                  # mapa del repositorio una vez por sesión (ctags)
            self._mapped = True
            try:
                rmap = devtools.repo_map(self.ws, budget=4000)
                extra += f"\n\nMapa del repositorio (símbolos principales):\n{rmap}"
            except Exception:  # noqa: BLE001
                pass
        if self.especialistas:
            extra += self.especialistas.hint(text)
        self.messages.append({"role": "user", "content": text + harness.hypothesis_note(text) + extra})
        self._evidence = set()
        if experience:
            self.emit("info", message=f"Usando {len(used_ids)} recuerdo(s) de tareas parecidas")
        self.current = self.router.first()
        loop = review.ReviewLoop()
        t0, tokens0 = time.monotonic(), self.stats["completion_tokens"]
        verified, last_verify, self_reviewed = None, "", False

        crit_rounds = rigor_rounds = 0
        while True:
            final = self._agent_loop()
            if self._cancel.is_set():
                break
            rigor = harness.rigor_check(final, self._evidence) if final else None
            if not rigor and final:
                bad = harness.grounding_check(final, self.ws)
                if bad:
                    rigor = ("Tu respuesta cita cosas que no se sostienen: " + "; ".join(bad[:6]) +
                             ". Revisa los archivos (read_file, simbolo) y corrige la respuesta; no inventes referencias.")
            if rigor and rigor_rounds < 2:
                rigor_rounds += 1
                self.stats["rigor_checks"] += 1
                self.emit("info", message="Control de rigor: la respuesta no estaba respaldada por datos, se exige verificarla")
                self.messages.append({"role": "user", "content": "[Control de rigor del arnés] " + rigor})
                continue
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
            if not (self.auto_verify and checks) or not self._ask("verify", {}, tools.preview(self.ws, "verify", {})):
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
        if verified and files:
            try:                      # nota legible de la mejora verificada en <proyecto>/.tisma/mejoras
                self.docs.save("mejora", task[:80], f"## Pedido\n{task}\n\n## Resultado\n{summary or '—'}\n\n"
                               f"## Archivos\n" + "\n".join(f"- `{f}`" for f in files) +
                               f"\n\n## Diff\n```diff\n{self.diff()[:6000]}\n```", ["verificado"])
            except (OSError, ValueError):
                pass
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
        role = self.especialistas.roles.get("compactar") if self.especialistas else None
        if role:
            summary = self.especialistas.chat(role, harness.compaction_prompt(middle))
        else:
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
            msg = self._call_llm(self.messages, self.schemas)
            self.stats["steps"] += 1
            content = msg.get("content") or ""
            calls = msg.get("tool_calls") or []
            if not calls and content:
                calls = harness.extract_text_tool_calls(content, self.tool_names)
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

    def _lint_feedback(self, target, before: str) -> str:
        """Linters sobre las líneas recién cambiadas: el modelo ve sus errores al instante."""
        try:
            rel = self.ws.rel(target)
            after = target.read_text(errors="replace")
            d = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{rel}", f"b/{rel}"))
            diags = devtools.diagnostics_near_changes(self.ws, rel, d)
        except Exception:  # noqa: BLE001 — un linter roto nunca debe romper la edición
            return ""
        if not diags:
            return ""
        self.stats["lint_feedback"] += 1
        return "\n\nLinters en las líneas cambiadas:\n" + devtools.format_diagnostics(diags, 15)

    def _run_call(self, call: dict) -> dict:
        fn = call.get("function", {})
        name = fn.get("name", "")
        cid = call.get("id") or uuid.uuid4().hex[:12]

        def reply(result: str, ok: bool, args: dict | None = None) -> dict:
            if ok:
                self._evidence.add(name)
                result = harness.untrusted_wrap(name, result)
            self.emit("tool", id=cid, tool=name, args=args or {}, result=tools._clip(result, 4000), ok=ok)
            return {"role": "tool", "tool_call_id": cid, "content": result}

        if name not in self.tool_names:
            return reply(f"Error: la herramienta '{name}' no existe. Disponibles: "
                         f"{', '.join(sorted(self.tool_names))}.", False)
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
        if name in ("docs_buscar", "docs_leer", "docs_guardar"):
            try:
                if name == "docs_buscar":
                    hits = self.docs.search(str(args.get("consulta", "")))
                    out = "\n\n".join(f"[{h['ref']}#{h['part']}] {h['title']}\n{h['snippet']}" for h in hits)
                    return reply(out or "Sin resultados en los documentos locales.", True, args)
                if name == "docs_leer":
                    part = args.get("part")
                    return reply(self.docs.read(str(args["ref"]), int(part) if part is not None else None), True, args)
                ref = self.docs.save(str(args["tipo"]), str(args["titulo"]), str(args["contenido"]),
                                     [str(t) for t in args.get("etiquetas", [])])
                self.emit("doc_saved", ref=ref, doc_kind=args["tipo"], title=args["titulo"])
                return reply(f"Guardado en {ref}", True, args)
            except (KeyError, ValueError, OSError) as e:
                return reply(f"Error: {e}", False, args)
        if name == "rea_guia":
            from .rea import guide
            return reply(guide(str(args.get("tema", ""))), True, args)
        if name in ("rea_buscar", "rea", "rea_modo"):
            try:
                from .rea import REATools
                if name == "rea_modo":
                    dyn, net = bool(args.get("dinamico")), bool(args.get("red"))
                    if (dyn or net) and not self._ask("rea_modo", args, tools.preview(self.ws, "rea_modo", args)):
                        return reply("El usuario no permitió cambiar el modo de REA.", False, args)
                    if self.rea is not None:
                        self.rea.close()
                    self.emit("info", message=f"REA en modo {'dinámico' if dyn else 'estático'}{' con red' if net else ''}…")
                    self.rea = REATools(self.ws, dynamic=dyn, network=net)
                    return reply(f"REA ahora en modo {self.rea.mode()}.", True, args)
                if self.rea is None:
                    self.emit("info", message="Iniciando REA (aislado, modo estático)…")
                    self.rea = REATools(self.ws)
                if name == "rea_buscar":
                    return reply(self.rea.search(str(args.get("consulta", ""))), True, args)
                tool = str(args.get("herramienta", ""))
                targs = args.get("argumentos") or {}
                if not isinstance(targs, dict):
                    return reply("Error: argumentos debe ser un objeto JSON", False, args)
                if self.rea.needs_approval(tool) and not self._ask("rea", args, tools.preview(self.ws, "rea", args)):
                    return reply("El usuario denegó esta acción.", False, args)
                return reply(tools._clip(self.rea.call(tool, targs), 12000), True, args)
            except Exception as e:  # noqa: BLE001 (MCPError, ausencia de REA…)
                return reply(f"Error en REA: {e}", False, args)
        if name == "especialista_buscar":
            return reply(self.especialistas.search(str(args.get("consulta", ""))), True, args)
        if name == "especialista":
            try:
                self.emit("info", message=f"Consultando al especialista {args.get('nombre')}…")
                return reply(tools._clip(self.especialistas.ask(str(args.get("nombre", "")),
                                                                str(args.get("entrada", ""))), 12000), True, args)
            except Exception as e:  # noqa: BLE001 (SpecialistError, LLMError, servidor no disponible…)
                return reply(f"Error del especialista: {e}", False, args)
        if name == "craft_guia":
            from .craft import guide as craft_guide
            return reply(craft_guide(str(args.get("tema", ""))), True, args)
        if name in ("craft_buscar", "craft", "craft_modo"):
            try:
                from .craft import CraftTools
                if name == "craft_modo":
                    modo, net = str(args.get("modo", "limpio")), bool(args.get("red"))
                    if (modo != "limpio" or net) and not self._ask("craft_modo", args,
                                                                   tools.preview(self.ws, "craft_modo", args)):
                        return reply("El usuario no permitió cambiar el modo de Craft Anything.", False, args)
                    if self.craft is not None:
                        self.craft.close()
                        self.craft = None
                    self.emit("info", message=f"Craft Anything en modo {modo}{' con red' if net else ''}…")
                    self.craft = CraftTools(self.ws, mode=modo, network=net)
                    return reply(f"Craft Anything ahora en modo {self.craft.mode()}.", True, args)
                if self.craft is None:
                    self.emit("info", message="Iniciando Craft Anything (aislado, modo limpio)…")
                    self.craft = CraftTools(self.ws)
                if name == "craft_buscar":
                    return reply(self.craft.search(str(args.get("consulta", ""))), True, args)
                tool = str(args.get("herramienta", ""))
                targs = args.get("argumentos") or {}
                if not isinstance(targs, dict):
                    return reply("Error: argumentos debe ser un objeto JSON", False, args)
                if self.craft.needs_approval(tool) and not self._ask("craft", args, tools.preview(self.ws, "craft", args)):
                    return reply("El usuario denegó esta acción.", False, args)
                return reply(tools._clip(self.craft.call(tool, targs), 12000), True, args)
            except Exception as e:  # noqa: BLE001 (MCPError, ausencia de Craft Anything…)
                return reply(f"Error en Craft Anything: {e}", False, args)
        if name == "jarvis":
            if not self._ask("jarvis", args, tools.preview(self.ws, "jarvis", args)):
                return reply("El usuario denegó esta acción.", False, args)
            from .jarvis import ask
            self.emit("info", message="Consultando a OpenJarvis…")
            return reply(tools._clip(ask(str(args.get("pregunta", ""))), 12000), True, args)
        if name == "update_todos":
            self.todos = [t for t in args.get("todos", []) if isinstance(t, dict)]
            self.emit("todos", todos=self.todos)
            return reply("Lista de tareas actualizada.", True, args)
        if name == "diagnosticos" and not args.get("rutas"):
            args["rutas"] = sorted(self._originals) or ["."]
        try:
            pv = tools.preview(self.ws, name, args)
            target = None
            if name in ("write_file", "edit_file", "formatear"):
                target = self.ws.resolve(args["path"])
                if name == "edit_file" and target.is_file() and args.get("old") not in target.read_text(errors="replace"):
                    self.stats["fuzzy_edits"] += 1
            if not self._ask(name, args, pv):
                return reply("El usuario denegó esta acción.", False, args)
            if target is not None:
                rel = self.ws.rel(target)
                if rel not in self._originals:
                    self._originals[rel] = target.read_text(errors="replace") if target.is_file() else ""
            before = target.read_text(errors="replace") if target is not None and target.is_file() else ""
            result = tools.execute(self.ws, name, args)
            if target is not None and name in ("write_file", "edit_file", "formatear"):
                result += self._lint_feedback(target, before)
            return reply(result, True, args)
        except (tools.ToolError, KeyError, OSError, ValueError) as e:
            return reply(f"Error: {e}", False, args)
