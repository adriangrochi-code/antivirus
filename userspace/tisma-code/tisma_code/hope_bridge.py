"""Puente con Hope: herramientas y estrategias de Hope sobre modelos locales (Strata / llama.cpp).

De Hope se usa todo lo que no es la red neuronal (vendor/hope, vendor/guardian):

* Catálogo de 53 herramientas ``⟦nombre: entrada⟧`` (cálculo exacto, unidades, ecuaciones,
  derivadas, ODEs, matrices, lógica z3, circuitos, filtros, ajuste, constantes, G-code, STL,
  Marlin, materiales, binarios, hash, SQL, gráficos, git…) con su ToolRunner y sandbox.
* Crítico CRE/HCRI (aritmética, unidades, dimensión, invariantes, datos faltantes o
  contradictorios) y Loop Controller (intentar → criticar → reintentar).
* Consejo de agentes (innovador, conservador, optimizado, veloz) con coordinador–verificador
  y UCB1 por tema; aquí cada agente es el modelo local con la «personalidad» del perfil.
* Base de conocimiento (soluciones verificadas y lecciones), cuaderno y TLM (memoria de uso
  de herramientas), forja de herramientas (el modelo propone, el sandbox valida).
"""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

VENDOR = next((p for p in (Path(__file__).resolve().parent.parent / "vendor",
                           Path("/usr/lib/centinela/python/vendor")) if (p / "hope").is_dir()), None)
if VENDOR and str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from hope.constraints.critic import RESULT_RE, critique  # noqa: E402
from hope.council.agents import PROFILES, Agent, AgentProfile  # noqa: E402
from hope.council.coordinator import AgentUsageMemory, Coordinator  # noqa: E402
from hope.memory.knowledge import KnowledgeBase, Notebook  # noqa: E402
from hope.memory.store import MemoryStore  # noqa: E402
from hope.runtime.loop import solve_with_review  # noqa: E402
from hope.runtime.toolcalls import CALL_RE, HELP, ToolRunner  # noqa: E402
from hope.teacher.client import OpenAICompatibleTeacher, TeacherConfig  # noqa: E402
from hope.tools.builtin import default_catalog  # noqa: E402
from hope.tools.creation import ToolForge  # noqa: E402
from hope.tools.tlm import ToolUsageMemory  # noqa: E402

STEP_FORMAT = ("Formato: un paso por línea «Paso n: expresión = valor»; al final una línea "
               "«Resultado: …». Si faltan datos o son contradictorios, dilo en el Resultado.")
# Herramientas que necesitan servicios que TISMA Code no conecta (web, wiki, navegador)
SKIP = {"buscar_web", "enlaces_web", "buscar_wiki", "inspeccionar", "leer_web"}


def tool_help(topic: str = "", limit: int = 60) -> str:
    words = set(re.findall(r"\w{3,}", topic.lower()))
    items = [(n, h) for n, h in HELP.items() if n not in SKIP]
    if words:
        scored = sorted(items, key=lambda kv: -sum(w in (kv[0] + " " + kv[1]).lower() for w in words))
        items = [kv for kv in scored if any(w in (kv[0] + " " + kv[1]).lower() for w in words)] or scored[:15]
    return "\n".join(f"- {n}: {h}" for n, h in items[:limit])


class HopeTools:
    """Una instancia por proyecto; la memoria y la TLM se guardan en su carpeta de datos."""

    def __init__(self, data_dir: Path, chat=None, endpoint: str = "http://127.0.0.1:8080/v1",
                 model: str = "local"):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.chat = chat                     # chat(messages, temperature) -> str
        self.kb = KnowledgeBase(MemoryStore(data_dir / "hope_memoria.db"))
        self.notebook = Notebook(self.kb)
        self.tlm = ToolUsageMemory(data_dir / "hope_tlm.json")
        self.catalog = default_catalog()
        self.teacher = OpenAICompatibleTeacher(TeacherConfig(endpoint=endpoint, model=model))
        self.forge = ToolForge(self.catalog, self.teacher, store_dir=data_dir / "herramientas_creadas")
        self.runner = ToolRunner(self.catalog, memory=self.notebook, forge=self.forge, tlm=self.tlm,
                                 teacher=self.teacher, topic="tisma-code")
        self.usage = AgentUsageMemory(data_dir / "hope_consejo.json")
        self._lock = threading.Lock()

    def set_endpoint(self, endpoint: str, model: str) -> None:
        self.teacher.config.endpoint, self.teacher.config.model = endpoint, model

    # ------------------------------------------------------------ herramientas
    def run(self, name: str, arg: str) -> str:
        if name in SKIP:
            return f"error: «{name}» no está disponible en TISMA Code (sin acceso web desde el agente)"
        with self._lock:
            return self.runner.run(name, arg)

    def run_inline(self, text: str) -> tuple[str, int]:
        """Ejecuta las llamadas ⟦…⟧ que aún no tienen resultado y agrega « → resultado»."""
        out, n, pos = [], 0, 0
        for m in CALL_RE.finditer(text):
            out.append(text[pos:m.end()])
            pos = m.end()
            if not text[pos:pos + 3].startswith(" →"):
                out.append(" → " + self.run(m.group(1), m.group(2)))
                n += 1
        out.append(text[pos:])
        return "".join(out), n

    # ------------------------------------------------------------ crítica
    @staticmethod
    def critique(problem: str, text: str) -> dict:
        c = critique(problem, text)
        return {"verdict": c.verdict, "feedback": c.feedback(), "issues": [i.detail for i in c.issues]}

    # ------------------------------------------------------------ consejo
    def _solver(self):
        def solve(topic: str, problem: str, profile: AgentProfile):
            def attempt(feedback: str):
                ctx = self.kb.context(topic, problem) if profile.use_memory else None
                memo = getattr(ctx, "text", "") if ctx else ""
                system = (f"Eres el agente «{profile.name}» del consejo. Objetivo: {profile.goal}. "
                          f"{profile.instruction} {STEP_FORMAT}")
                if profile.use_tools:
                    system += ("\nPuedes usar herramientas exactas escribiendo ⟦nombre: entrada⟧; el sistema "
                               "agrega « → resultado». Útiles para este tema:\n" + tool_help(problem, 12))
                user = problem + (f"\n\n{memo}" if memo else "") + (f"\n\n{feedback}" if feedback else "")
                msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
                text = self.chat(msgs, profile.temperature)
                for _ in range(3):                     # resultados de herramientas → el agente rehace
                    done, n = self.run_inline(text) if profile.use_tools else (text, 0)
                    if not n:
                        break
                    msgs += [{"role": "assistant", "content": done},
                             {"role": "user", "content": "Ya tienes los resultados de las herramientas (→). "
                              "Escribe la solución final con esos valores, en el formato pedido."}]
                    text = self.chat(msgs, profile.temperature)
                m = RESULT_RE.search(text)
                return text, (m.group(1).strip() if m else None)

            res = solve_with_review(attempt, lambda t, a: critique(problem, t, a),
                                    max_iterations=max(1, profile.review_iterations))
            return res.text, res.answer
        return solve

    def council(self, problem: str, topic: str = "general", k: int | None = None) -> dict:
        t0 = time.monotonic()
        solver = self._solver()
        coord = Coordinator([Agent(p, solver) for p in PROFILES.values()], memory=self.usage)
        r = coord.solve(topic, problem, k=k)
        if r.chosen:
            ok = r.chosen.verdict == "ok"
            self.kb.learn_solution(topic, problem, r.chosen.candidate.text, r.answer, verified=ok)
            for s in r.ranking:
                if s.verdict == "errores" and s.issues:
                    self.kb.learn_lesson(topic, problem, s.candidate.answer, s.issues)
        return {
            "answer": r.answer,
            "verdict": r.chosen.verdict if r.chosen else "sin_respuesta",
            "chosen_agent": r.chosen.candidate.agent if r.chosen else None,
            "reasoning": r.chosen.candidate.text if r.chosen else "",
            "ranking": [{"agent": s.candidate.agent, "verdict": s.verdict, "answer": s.candidate.answer,
                         "score": round(s.score, 3), "issues": s.issues} for s in r.ranking],
            "reasons": r.reasons, "seconds": round(time.monotonic() - t0, 1),
        }

    def sleep(self) -> dict:
        """«Sueño»: consolidación de la memoria (lo usado y respaldado se afianza, el ruido se archiva)."""
        with self._lock:
            return self.kb.optimize()
