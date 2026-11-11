"""Herramienta `ingenieria`: recetas fijas (sin inyección), rutas dentro del proyecto y resumen de números."""
import json
import shlex
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import ingenieria, tools  # noqa: E402


@pytest.fixture
def ws(tmp_path, monkeypatch):
    w = tools.Workspace(str(tmp_path))
    w.calls = []
    w.out = "[código de salida 0]\n"
    monkeypatch.setattr(w, "run_command", lambda cmd, timeout=120: w.calls.append((cmd, timeout)) or w.out)
    monkeypatch.setattr(ingenieria.shutil, "which", lambda b: "/usr/bin/" + b)
    return w


def test_registered_and_needs_approval():
    assert {"ingenieria", "ingenieria_guia"} <= tools.TOOL_NAMES
    assert "ingenieria" in tools.NEEDS_APPROVAL and "ingenieria_guia" not in tools.NEEDS_APPROVAL


def test_openscad_recipe_quotes_names_and_parses_mesh(ws):
    (ws.root / "pieza; rm -rf ~.scad").write_text("cube(20);")
    ws.out = '[código de salida 0]\n{"triangulos": 12, "estanca": true, "volumen_mm3": 8000.0, "area_mm2": 2400.0, ' \
             '"caja_min": [0, 0, 0], "caja_max": [20, 20, 20], "centro_masa": [10, 10, 10]}'
    r = tools.execute(ws, "ingenieria", {"accion": "openscad", "archivo": "pieza; rm -rf ~.scad"})
    assert r.startswith("Malla: 12 triángulos, estanca=True, volumen=8000.0 mm³")
    cmd, _ = ws.calls[0]
    assert shlex.split(cmd)[:5] == ["openscad", "-o", "pieza; rm -rf ~.stl", "--export-format", "binstl"]
    assert (ws.root / ".tisma").is_dir()


def test_rejects_outside_project_wrong_extension_and_bad_options(ws, tmp_path):
    (ws.root / "a.geo").write_text("")
    (ws.root / "t.v").write_text("")
    for args in ({"accion": "gmsh", "archivo": "../x.geo"},
                 {"accion": "gmsh", "archivo": "/etc/passwd"},
                 {"accion": "gmsh", "archivo": "a.v"},
                 {"accion": "gmsh", "archivo": "a.geo", "opciones": {"dimension": 7}},
                 {"accion": "yosys", "archivo": "t.v", "opciones": {"top": "x; system rm"}},
                 {"accion": "openfoam", "archivo": ".", "opciones": {"solver": "$(reboot)"}},
                 {"accion": "pandoc", "archivo": "a.md", "opciones": {"formato": "sh"}},
                 {"accion": "bash", "archivo": "a.geo"},
                 {"accion": "verilog", "archivo": "t.v", "opciones": {"tb": "/etc/passwd"}},
                 {"accion": "gmsh", "archivo": "a.geo", "salida": "../fuera.msh"}):
        with pytest.raises(tools.ToolError):
            tools.execute(ws, "ingenieria", args)
    assert ws.calls == []


def test_planted_tisma_symlink_rejected(ws, tmp_path):
    (ws.root / "a.geo").write_text("")
    out = tmp_path.parent / "fuera"
    out.mkdir(exist_ok=True)
    (ws.root / ".tisma").symlink_to(out)
    with pytest.raises(tools.ToolError):
        tools.execute(ws, "ingenieria", {"accion": "gmsh", "archivo": "a.geo"})


def test_summaries(ws):
    (ws.root / "c.cir").write_text("")
    ws.out = "Circuit: rc\nNo. of Data Rows : 10\ntau                 =  1.000000e-03 targ=  1.0e-03\nvmax = 4.99e+00\n"
    r = tools.execute(ws, "ingenieria", {"accion": "ngspice", "archivo": "c.cir"})
    assert r.splitlines()[:2] == ["tau                 =  1.000000e-03 targ=  1.0e-03", "vmax = 4.99e+00"]
    (ws.root / "p.kicad_pcb").write_text("")
    ws.out = "[código de salida 5]\n" + json.dumps({"violations": [{"severity": "error", "description": "Clearance"}],
                                                   "unconnected_items": [{"severity": "error", "description": "GND"}]})
    r = tools.execute(ws, "ingenieria", {"accion": "kicad_drc", "archivo": "p.kicad_pcb"})
    assert r.startswith("2 incidencia(s)\n- [error] Clearance")
    (ws.root / "m.geo").write_text("")
    ws.out = "Info    : Done meshing 3D\nInfo    : 1234 nodes 5678 elements\n"
    assert "1234 nodos, 5678 elementos" in tools.execute(ws, "ingenieria", {"accion": "gmsh", "archivo": "m.geo"})


def test_openfoam_needs_case_and_has_long_timeout(ws):
    with pytest.raises(tools.ToolError):
        tools.execute(ws, "ingenieria", {"accion": "openfoam", "archivo": "."})
    (ws.root / "caso" / "system").mkdir(parents=True)
    (ws.root / "caso" / "system" / "controlDict").write_text("application simpleFoam;")
    ws.out = "Mesh OK.\nTime = 100\nEnd\n"
    r = tools.execute(ws, "ingenieria", {"accion": "openfoam", "archivo": "caso", "opciones": {"solver": "simpleFoam"}})
    assert "Mesh OK." in r and ws.calls[0][1] == 1800 and "simpleFoam > log.solver" in ws.calls[0][0]


def test_missing_program_is_explained(ws, monkeypatch):
    monkeypatch.setattr(ingenieria.shutil, "which", lambda b: None)
    (ws.root / "c").mkdir()
    with pytest.raises(tools.ToolError, match="centinela-apps simulacion"):
        tools.execute(ws, "ingenieria", {"accion": "openfoam", "archivo": "c"})
    assert "Instaladas" in tools.execute(ws, "ingenieria_guia", {})


@pytest.mark.skipif(not (shutil.which("bwrap") and shutil.which("pandoc")), reason="sin bwrap/pandoc")
def test_real_pandoc_in_jail(tmp_path):
    w = tools.Workspace(str(tmp_path))
    (tmp_path / "informe.md").write_text("# Resultado\n\nVolumen: 8000 mm³\n")
    r = tools.execute(w, "ingenieria", {"accion": "pandoc", "archivo": "informe.md", "opciones": {"formato": "html"}})
    if "código de salida 0" not in r:
        pytest.skip(f"bubblewrap no funciona aquí: {r[:200]}")
    assert "8000 mm³" in (tmp_path / "informe.html").read_text()


def test_yosys_file_is_not_inside_script(ws):
    (ws.root / "x;shell rm.v").write_text("")
    tools.execute(ws, "ingenieria", {"accion": "yosys", "archivo": "x;shell rm.v"})
    argv = shlex.split(ws.calls[0][0])
    assert argv[:4] == ["yosys", "-q", "-p", "synth -auto-top; stat"] and argv[-1] == "x;shell rm.v"
