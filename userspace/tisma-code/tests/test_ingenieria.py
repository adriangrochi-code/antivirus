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
    assert r.splitlines()[:2] == ["tau = 0.001", "vmax = 4.99"]
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
    with pytest.raises(tools.ToolError, match="Componentes → Simulación") as e:      # un botón, no una orden
        tools.execute(ws, "ingenieria", {"accion": "openfoam", "archivo": "c"})
    assert "sudo" not in str(e.value)
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
    assert argv[:4] == ["yosys", "-q", "-p", "synth -auto-top; tee -q -o /dev/stdout stat"] and argv[-1] == "x;shell rm.v"


def test_check_reports_discrepancies():
    r = ingenieria.check({"volumen_mm3": 7623.5, "estanca": True}, {"volumen_mm3": 7680, "estanca": True, "nodos": 5}, tol=0.005)
    assert "2 DISCREPANCIA(S)" in r and "volumen_mm3: DISCREPANCIA" in r and "-0.74%" in r and "nodos: SIN MEDIR" in r
    assert ingenieria.check({"fc": 159.155}, {"fc": 159.15}).startswith("COMPROBACIÓN: todo coincide")


def test_template_copy_never_overwrites_or_escapes(tmp_path):
    ws = tools.Workspace(str(tmp_path))
    assert "openfoam/cavidad" in tools.execute(ws, "ingenieria_plantilla", {})
    out = tools.execute(ws, "ingenieria_plantilla", {"nombre": "verilog/contador.v", "destino": "hdl/contador.v"})
    assert (tmp_path / "hdl" / "contador_tb.v").is_file() and "hdl/contador.v" in out
    with pytest.raises(tools.ToolError, match="no se sobrescribe"):
        tools.execute(ws, "ingenieria_plantilla", {"nombre": "verilog/contador.v", "destino": "hdl/contador.v"})
    for bad in ({"nombre": "../../etc/passwd"}, {"nombre": "octave/viga.m", "destino": "../fuera.m"}):
        with pytest.raises(tools.ToolError):
            tools.execute(ws, "ingenieria_plantilla", bad)
    (tmp_path / "enlace").symlink_to(tmp_path.parent)
    with pytest.raises(tools.ToolError):
        tools.execute(ws, "ingenieria_plantilla", {"nombre": "octave/viga.m", "destino": "enlace/viga.m"})


def test_stl_analyzer_without_trimesh(tmp_path):
    import struct
    import subprocess
    # tetraedro unitario (estanco, volumen 1/6) en STL binario
    v = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
    faces = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]
    data = b"\0" * 80 + struct.pack("<I", 4) + b"".join(
        struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[c], 0) for a, b, c in faces)
    (tmp_path / "t.stl").write_bytes(data)
    out = subprocess.run([sys.executable, "-c", ingenieria.MALLA_PY.replace("import trimesh", "raise ImportError"),
                          str(tmp_path / "t.stl")], capture_output=True, text=True).stdout
    d = json.loads(out)
    assert d["estanca"] and abs(d["volumen_mm3"] - 1 / 6) < 1e-3 and d["triangulos"] == 4
    (tmp_path / "abierta.stl").write_bytes(data[:84 - 4] + struct.pack("<I", 3) + data[84:84 + 150])
    d = json.loads(subprocess.run([sys.executable, "-c", ingenieria.MALLA_PY.replace("import trimesh", "raise ImportError"),
                                   str(tmp_path / "abierta.stl")], capture_output=True, text=True).stdout)
    assert not d["estanca"] and d["volumen_mm3"] is None


REAL = [("openscad", "openscad/soporte.scad", "soporte.scad", {"volumen_mm3": 7623.5, "tam_x": 60, "estanca": True}),
        ("ngspice", "ngspice/filtro_rc.cir", "filtro_rc.cir", {"fc": 159.15, "v_tau": 0.632}),
        ("verilog", "verilog/contador.v", "contador.v", {"pasa": True}),
        ("yosys", "verilog/contador.v", "contador.v", {"celdas": 12}),
        ("gmsh", "gmsh/placa_agujero.geo", "placa_agujero.geo", {}),
        ("octave", "octave/viga.m", "viga.m", {"flecha_max_mm": 1.157, "sigma_max_MPa": 20.83}),
        ("openfoam", "openfoam/cavidad", "cavidad", {"malla_ok": True, "termino": True, "tiempo_final": 0.5})]


@pytest.mark.parametrize("accion,plantilla,archivo,esperado", REAL, ids=[r[0] for r in REAL])
def test_templates_really_run(tmp_path, accion, plantilla, archivo, esperado):
    """Ejecuta cada plantilla con el programa real (en la jaula) y comprueba los números calculados a mano."""
    if not (shutil.which("bwrap") and shutil.which(ingenieria.BIN[accion])):
        pytest.skip(f"{ingenieria.BIN[accion]} no instalado")
    ws = tools.Workspace(str(tmp_path))
    tools.execute(ws, "ingenieria_plantilla", {"nombre": plantilla})
    opts = {"esperado": esperado} if esperado else {}
    if accion == "verilog":
        opts["tb"] = "contador_tb.v"
    if accion == "gmsh":
        opts["dimension"] = 2
    r = tools.execute(ws, "ingenieria", {"accion": accion, "archivo": archivo, "opciones": opts})
    if "No se pudo" in r or "bwrap:" in r:
        pytest.skip(f"jaula no disponible: {r[:200]}")
    if esperado:
        assert r.startswith("COMPROBACIÓN: todo coincide"), r[:1500]
    else:
        assert "nodos" in r, r[:1500]
