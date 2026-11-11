"""Herramientas de desarrollo open source para el arnés.

Un modelo (sobre todo uno pequeño) rinde mucho más si recibe información exacta en lugar de
adivinarla: dónde está definido un símbolo (universal-ctags), qué errores reales tiene el código
recién editado (ruff, shellcheck, cppcheck, eslint, mypy, bandit…), búsqueda rápida (ripgrep),
formato determinista (ruff format, clang-format, shfmt) e historial (git).

Seguridad: varios de estos programas pueden ejecutar código del repositorio (configuraciones de
eslint en JS, ``core.fsmonitor``/``diff.external`` en .git/config…). Por eso TODOS se ejecutan con
``Workspace.sandbox_argv``: bubblewrap, sistema de solo lectura, sin red y sin ver ``$HOME``.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from typing import TYPE_CHECKING

from .base import ToolError, _clip

if TYPE_CHECKING:
    from .tools import Workspace

TIMEOUT = 60


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _run(ws: Workspace, argv: list[str], stdin: str | None = None, timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    full = ws.sandbox_argv(shlex.join(argv))
    return subprocess.run(full, cwd=ws.root, input=stdin, capture_output=True, text=True,
                          timeout=timeout, errors="replace")


# ---------------------------------------------------------------- diagnósticos
def _ruff(ws, files):
    r = _run(ws, ["ruff", "check", "--output-format=json", "--no-cache", "--", *files])
    out = []
    for d in json.loads(r.stdout or "[]"):
        out.append({"file": d["filename"], "line": d["location"]["row"], "col": d["location"]["column"],
                    "severity": "error" if d.get("code", "").startswith(("E9", "F")) else "warning",
                    "code": d.get("code") or "", "msg": d["message"], "tool": "ruff"})
    return out


def _mypy(ws, files):
    r = _run(ws, ["mypy", "--no-error-summary", "--show-column-numbers", "--ignore-missing-imports",
                  "--no-incremental", "--", *files], timeout=120)
    out = []
    for m in re.finditer(r"^(.+?):(\d+):(\d+): (error|note|warning): (.+?)(?:  \[(.+)\])?$", r.stdout, re.M):
        if m.group(4) == "note":
            continue
        out.append({"file": m.group(1), "line": int(m.group(2)), "col": int(m.group(3)), "severity": m.group(4),
                    "code": m.group(6) or "", "msg": m.group(5), "tool": "mypy"})
    return out


def _bandit(ws, files):
    r = _run(ws, ["bandit", "-q", "-f", "json", "--", *files])
    out = []
    for d in json.loads(r.stdout or "{}").get("results", []):
        out.append({"file": d["filename"], "line": d["line_number"], "col": 0,
                    "severity": "error" if d["issue_severity"] == "HIGH" else "warning",
                    "code": d["test_id"], "msg": "seguridad: " + d["issue_text"], "tool": "bandit"})
    return out


def _shellcheck(ws, files):
    r = _run(ws, ["shellcheck", "-f", "json", "--", *files])
    sev = {"error": "error", "warning": "warning", "info": "info", "style": "info"}
    return [{"file": d["file"], "line": d["line"], "col": d["column"], "severity": sev.get(d["level"], "info"),
             "code": f"SC{d['code']}", "msg": d["message"], "tool": "shellcheck"} for d in json.loads(r.stdout or "[]")]


def _cppcheck(ws, files):
    r = _run(ws, ["cppcheck", "--quiet", "--enable=warning,portability,performance",
                  "--template={file}\t{line}\t{column}\t{severity}\t{id}\t{message}", "--", *files], timeout=120)
    out = []
    for line in r.stderr.splitlines():
        p = line.split("\t")
        if len(p) == 6 and p[1].isdigit():
            out.append({"file": p[0], "line": int(p[1]), "col": int(p[2] or 0),
                        "severity": "error" if p[3] == "error" else "warning", "code": p[4], "msg": p[5],
                        "tool": "cppcheck"})
    return out


def _eslint(ws, files):
    if not any((ws.root / n).exists() for n in ("eslint.config.js", "eslint.config.mjs", ".eslintrc.json",
                                                ".eslintrc.js", ".eslintrc.cjs", ".eslintrc")):
        return []
    r = _run(ws, ["eslint", "-f", "json", "--", *files], timeout=120)
    out = []
    for f in json.loads(r.stdout or "[]"):
        for m in f.get("messages", []):
            out.append({"file": f["filePath"], "line": m.get("line", 0), "col": m.get("column", 0),
                        "severity": "error" if m.get("severity") == 2 else "warning",
                        "code": m.get("ruleId") or "", "msg": m["message"], "tool": "eslint"})
    return out


def _json_check(ws, files):
    out = []
    for f in files:
        try:
            json.loads((ws.root / f).read_text(encoding="utf-8"))
        except ValueError as e:
            out.append({"file": f, "line": getattr(e, "lineno", 0), "col": getattr(e, "colno", 0),
                        "severity": "error", "code": "json", "msg": str(e), "tool": "json"})
    return out


# extensión → (linters rápidos tras cada edición, linters extra del modo completo)
LINTERS = {
    ".py": ([("ruff", _ruff)], [("mypy", _mypy), ("bandit", _bandit)]),
    ".sh": ([("shellcheck", _shellcheck)], []), ".bash": ([("shellcheck", _shellcheck)], []),
    ".c": ([("cppcheck", _cppcheck)], []), ".h": ([("cppcheck", _cppcheck)], []),
    ".cpp": ([("cppcheck", _cppcheck)], []), ".hpp": ([("cppcheck", _cppcheck)], []),
    ".cc": ([("cppcheck", _cppcheck)], []),
    ".js": ([("eslint", _eslint)], []), ".mjs": ([("eslint", _eslint)], []), ".ts": ([("eslint", _eslint)], []),
    ".tsx": ([("eslint", _eslint)], []), ".jsx": ([("eslint", _eslint)], []),
    ".json": ([("python3", _json_check)], []),
}


def diagnostics(ws: Workspace, files: list[str], full: bool = False) -> list[dict]:
    by_ext: dict[str, list[str]] = defaultdict(list)
    for f in files:
        rel = ws.rel(ws.resolve(f))
        by_ext[Path(rel).suffix.lower()].append(rel)
    out: list[dict] = []
    for ext, fs in by_ext.items():
        fast, extra = LINTERS.get(ext, ([], []))
        for tool, fn in fast + (extra if full else []):
            if not have(tool):
                continue
            try:
                for d in fn(ws, fs):
                    try:
                        d["file"] = ws.rel(ws.resolve(d["file"]))
                    except ToolError:
                        continue
                    out.append(d)
            except (subprocess.SubprocessError, ValueError, KeyError, OSError):
                continue
    return sorted(out, key=lambda d: (d["file"], d["line"]))


def format_diagnostics(diags: list[dict], limit: int = 40) -> str:
    if not diags:
        return "Sin problemas."
    errs = sum(d["severity"] == "error" for d in diags)
    lines = [f"{len(diags)} problema(s), {errs} error(es):"]
    for d in diags[:limit]:
        lines.append(f"{d['file']}:{d['line']}:{d['col']}: {d['severity']} [{d['tool']} {d['code']}] {d['msg']}")
    if len(diags) > limit:
        lines.append(f"… y {len(diags) - limit} más")
    return "\n".join(lines)


def changed_lines(diff_text: str) -> dict[str, set[int]]:
    """Líneas nuevas/modificadas por archivo según un diff unificado."""
    out: dict[str, set[int]] = defaultdict(set)
    cur, n = None, 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            cur = line[4:].removeprefix("b/")
        elif line.startswith("@@") and cur:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            n = int(m.group(1)) if m else 0
        elif cur and line.startswith("+"):
            out[cur].add(n)
            n += 1
        elif cur and not line.startswith("-"):
            n += 1
    return out


def diagnostics_near_changes(ws: Workspace, rel: str, diff_text: str, margin: int = 2) -> list[dict]:
    """Diagnósticos del archivo editado que caen en (o junto a) las líneas cambiadas,
    más cualquier error de sintaxis del archivo (rompe todo lo demás)."""
    touched = changed_lines(diff_text).get(rel, set())
    res = []
    for d in diagnostics(ws, [rel]):
        near = any(abs(d["line"] - t) <= margin for t in touched)
        syntax = d["code"] in ("E999", "invalid-syntax", "syntax", "json") or "SyntaxError" in d["msg"]
        if near or syntax or not touched:
            res.append(d)
    return res


# ---------------------------------------------------------------- búsqueda y símbolos
def rg_search(ws: Workspace, pattern: str, glob: str = "*", max_results: int = 200) -> str:
    argv = ["rg", "--line-number", "--no-heading", "--color=never", "--max-columns=240",
            "--max-columns-preview", "--smart-case", "--glob", "!.tisma"]
    if glob and glob != "*":
        argv += ["--glob", glob]
    argv += ["--regexp", pattern, "."]
    r = _run(ws, argv)
    if r.returncode not in (0, 1):
        raise ToolError(r.stderr.strip()[:300] or "error de ripgrep")
    lines = r.stdout.splitlines()
    more = f"\n… ({len(lines) - max_results} resultados más; afina el patrón)" if len(lines) > max_results else ""
    return "\n".join(lines[:max_results]) + more if lines else "Sin resultados"


def _tags(ws: Workspace) -> list[dict]:
    r = _run(ws, ["ctags", "-R", "--output-format=json", "--fields=+nKS-P", "--extras=-F",
                  "--exclude=.git", "--exclude=node_modules", "--exclude=.tisma", "--exclude=build",
                  "--exclude=dist", "--exclude=.venv", "--exclude=venv", "--exclude=target", "-f", "-", "."],
             timeout=120)
    tags = []
    for line in r.stdout.splitlines():
        try:
            t = json.loads(line)
        except ValueError:
            continue
        if t.get("_type") == "tag":
            t["path"] = t["path"].removeprefix("./")
            tags.append(t)
    return tags


SKIP_LANGS = {"Make", "Markdown", "JSON", "Yaml", "Iniconf", "Automake", "CMake", "Maven2"}
IMPORTANT = {"class", "function", "method", "struct", "interface", "enum", "macro", "typedef",
             "module", "namespace", "func", "type", "trait", "impl"}


def repo_map(ws: Workspace, budget: int = 6000) -> str:
    """Mapa compacto del repositorio: por archivo, sus clases/funciones con firma y línea."""
    if not have("ctags"):
        return ws.list_dir(".", 3)
    by_file: dict[str, list[str]] = defaultdict(list)
    for t in _tags(ws):
        if t.get("kind") not in IMPORTANT or t.get("language") in SKIP_LANGS or t["name"].endswith("="):
            continue
        scope = f"{t['scope']}." if t.get("scope") and t.get("kind") == "method" else ""
        sig = t.get("signature", "")
        by_file[t["path"]].append(f"  {t['kind'][:5]:<5} {scope}{t['name']}{sig[:80]}  :{t.get('line', '?')}")
    out, used = [], 0
    for path in sorted(by_file, key=lambda p: (p.count("/"), p)):     # primero lo de la raíz
        block = path + "\n" + "\n".join(by_file[path][:40]) + "\n"
        if used + len(block) > budget:
            out.append(f"… ({len(by_file) - len(out)} archivos más; usa simbolo o buscar)")
            break
        out.append(block)
        used += len(block)
    return "".join(out) or "(sin símbolos reconocidos)"


def find_symbol(ws: Workspace, name: str) -> str:
    if not have("ctags"):
        return rg_search(ws, rf"\b(def|class|function|func|fn|struct)\s+{re.escape(name)}\b")
    hits = [t for t in _tags(ws) if t["name"] == name or t["name"].endswith("." + name)]
    if not hits:
        return f"No se encontró la definición de «{name}». Prueba con buscar."
    out = []
    for t in hits[:8]:
        line = int(t.get("line", 1))
        try:
            src = (ws.root / t["path"]).read_text(errors="replace").splitlines()
            snippet = "\n".join(f"{i:>5}\t{src[i - 1]}" for i in range(line, min(line + 12, len(src) + 1)))
        except OSError:
            snippet = ""
        out.append(f"{t['path']}:{line} ({t.get('kind')})\n{snippet}")
    return "\n\n".join(out)


# ---------------------------------------------------------------- formato
FORMATTERS = {
    ".py": ["ruff", "format", "--stdin-filename", "{path}", "-"],
    ".c": ["clang-format", "--assume-filename={path}"], ".h": ["clang-format", "--assume-filename={path}"],
    ".cpp": ["clang-format", "--assume-filename={path}"], ".hpp": ["clang-format", "--assume-filename={path}"],
    ".cc": ["clang-format", "--assume-filename={path}"], ".java": ["clang-format", "--assume-filename={path}"],
    ".sh": ["shfmt", "-i", "4", "-"], ".bash": ["shfmt", "-i", "4", "-"],
}


def formatted(ws: Workspace, path: str) -> tuple[Path, str, str]:
    p = ws.resolve(path)
    tpl = FORMATTERS.get(p.suffix.lower())
    if not tpl or not have(tpl[0]):
        raise ToolError(f"No hay formateador disponible para {p.suffix or 'este archivo'}")
    rel = ws.rel(p)
    argv = [a.replace("{path}", rel) for a in tpl]
    old = p.read_text(encoding="utf-8")
    r = _run(ws, argv, stdin=old)
    if r.returncode != 0:
        raise ToolError(f"El formateador falló: {r.stderr.strip()[:300]}")
    return p, old, r.stdout


# ---------------------------------------------------------------- git (solo lectura)
GIT_ALLOWED = {"status", "diff", "log", "show", "blame", "branch", "ls-files", "shortlog", "grep", "rev-parse"}
# LISTA BLANCA de opciones (una lista negra no sirve: git acepta abreviaturas, «--outp» = «--output», y opciones
# como «-O» / «--open-files-in-pager» de git grep ejecutan un programa). Lo que no empiece por «-» (revisiones,
# rutas, patrones) se permite; tras «--» todo son rutas.
GIT_SAFE_LONG = {
    "--stat", "--shortstat", "--numstat", "--summary", "--name-only", "--name-status", "--oneline", "--graph",
    "--decorate", "--all", "--reverse", "--first-parent", "--merges", "--no-merges", "--follow", "--patch",
    "--cached", "--staged", "--word-diff", "--ignore-all-space", "--ignore-space-change", "--find-renames",
    "--diff-filter", "--relative", "--root", "--format", "--pretty", "--date", "--since", "--until", "--after",
    "--before", "--author", "--committer", "--grep", "--max-count", "--skip", "--abbrev-commit", "--porcelain",
    "--short", "--branch", "--untracked-files", "--others", "--exclude-standard", "--count", "--heading",
    "--break", "--line-number", "--ignore-case", "--fixed-strings", "--extended-regexp", "--basic-regexp",
    "--perl-regexp", "--word-regexp", "--invert-match", "--files-with-matches", "--files-without-match",
    "--and", "--or", "--not", "--all-match", "--show-function", "--function-context", "--context",
    "--after-context", "--before-context", "--max-depth", "--abbrev-ref", "--show-toplevel", "--verify",
    "--show-current", "--list", "--remotes", "--contains", "--merged", "--no-merged", "--sort", "--unified",
    "--color=never", "--no-color", "--full-history", "--simplify-merges", "--left-right", "--cherry-pick",
    "--show-signature", "--line-porcelain", "--incremental", "--ignore-rev", "--deleted", "--modified",
    "--stage", "--error-unmatch", "--full-name", "--exclude", "--text", "--minimal", "--histogram", "--patience",
}
GIT_FLAG_SHORT = set("nipwlvEFsbIhHLMrzRaqtuNPG")         # sin argumento (se permiten agrupadas: -in)
GIT_VALUE_SHORT = set("UABCSGe")                          # con valor pegado o separado: -U3, -S texto, -e patrón


def _git_arg_ok(a: str) -> bool:
    if not a.startswith("-") or a == "-":
        return True
    if a.startswith("--"):
        name = a.split("=", 1)[0]
        return name in GIT_SAFE_LONG or a in GIT_SAFE_LONG
    body = a[1:]
    if body.isdigit():                                    # -5 (últimos 5 commits)
        return True
    if body[0] in GIT_VALUE_SHORT:
        return True                                       # el resto es el valor de la opción
    return all(ch in GIT_FLAG_SHORT for ch in body)


def git(ws: Workspace, sub: str, args: list[str] | None = None) -> str:
    args = [str(a) for a in (args or [])]
    if sub not in GIT_ALLOWED:
        raise ToolError(f"Subcomando no permitido: {sub}. Solo lectura: {', '.join(sorted(GIT_ALLOWED))}")
    if sub == "branch" and any(not a.startswith(("--list", "-a", "-v", "--show-current", "-r")) for a in args):
        raise ToolError("git branch solo para listar")
    paths = False
    for a in args:
        if a == "--":
            paths = True
            continue
        if not paths and not _git_arg_ok(a):
            raise ToolError(f"Opción de git no permitida: {a} (la herramienta git es de solo lectura)")
    argv = ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
            "-c", "diff.external=", "-c", "core.pager=cat", "-c", "protocol.allow=never", sub]
    if sub in ("diff", "show", "log"):
        argv += ["--no-ext-diff", "--no-textconv", "--no-color"]
    r = _run(ws, argv + args)
    if r.returncode != 0:
        raise ToolError(r.stderr.strip()[:500] or "error de git")
    return _clip(r.stdout or "(sin salida)", 16000)


SCHEMAS = [
    {"type": "function", "function": {
        "name": "mapa_repo", "description": "Mapa del repositorio: archivos con sus clases y funciones (firma y "
        "línea). Úsalo al empezar para orientarte sin leer todos los archivos.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "simbolo", "description": "Dónde está definida una función, clase o método (con su código).",
        "parameters": {"type": "object", "required": ["nombre"], "properties": {"nombre": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "diagnosticos", "description": "Errores reales del código según linters (ruff, shellcheck, "
        "cppcheck, eslint…; con completo=true también mypy y bandit de seguridad).",
        "parameters": {"type": "object", "properties": {
            "rutas": {"type": "array", "items": {"type": "string"}, "description": "archivos; vacío = cambiados"},
            "completo": {"type": "boolean"}}}}},
    {"type": "function", "function": {
        "name": "formatear", "description": "Da formato estándar a un archivo (ruff format, clang-format, shfmt).",
        "parameters": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "git", "description": "git de solo lectura: status, diff, log, show, blame, branch, ls-files, grep.",
        "parameters": {"type": "object", "required": ["subcomando"], "properties": {
            "subcomando": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}}}}},
]
