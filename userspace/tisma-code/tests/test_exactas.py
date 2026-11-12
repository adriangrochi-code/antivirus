"""Herramientas de cálculo exacto propias (respaldo sin Hope): resultados exactos, seguridad y jaula."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import exactas, kits, tools  # noqa: E402

WORKER = exactas.WORKER


def w(herramienta, **args):
    """El worker directo (sin jaula): la lógica de cálculo."""
    r = subprocess.run([sys.executable, "-I", str(WORKER)], input=json.dumps({"herramienta": herramienta, "args": args}),
                       capture_output=True, text=True, timeout=120)
    d = json.loads(r.stdout)
    return d["resultado"], d["ok"]


def _modulo(n):
    try:
        __import__(n)
        return True
    except ImportError:
        return False


def _bwrap_ok():
    return bool(shutil.which("bwrap")) and subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"],
                                                          capture_output=True).returncode == 0


@pytest.mark.parametrize("expr, esperado", [
    ("0.1 + 0.2 == 0.3", "verdadero"),           # exacto: no es el 0.30000000000000004 de un double
    ("1/3 + 1/6", "= 1/2"),
    ("2^100", "1267650600228229401496703205376"),
    ("8^(2/3)", "= 4"),
    ("3,5 * 2", "= 7"),                          # coma decimal
    ("15% * 200", "= 30"),
    ("7 % 3", "= 1"),                            # módulo, no porcentaje
    ("factores(2**64 + 1)", "274177 · 67280421310721"),
    ("primo(2**61 - 1)", "verdadero"),
    ("0xFF & 0b1010", "= 10"),
    ("comb(52, 5)", "2598960"),
])
def test_calcular_exacto(expr, esperado):
    out, ok = w("calcular", expresion=expr)
    assert ok and esperado in out, out


@pytest.mark.skipif(not _modulo("mpmath"), reason="sin mpmath")
def test_irracionales_con_las_cifras_pedidas():
    out, ok = w("calcular", expresion="sqrt(2)", digitos=50)
    assert ok and "1.4142135623730950488016887242096980785696718753769" in out and "50 cifras" in out
    out, _ = w("calcular", expresion="sqrt(2)^2 == 2")
    assert "verdadero" in out and "irracionales" in out
    out, _ = w("calcular", expresion="1/7")
    assert "periódico" in out


@pytest.mark.parametrize("expr", ["__import__('os').system('id')", "open('/etc/passwd')", "().__class__",
                                  "[x for x in range(10)]", "lambda: 1", "(1).real"])
def test_calcular_no_ejecuta_codigo(expr):
    out, ok = w("calcular", expresion=expr)
    assert not ok


@pytest.mark.parametrize("expr", ["10**10**10", "factorial(10**9)", "1 << 10**9"])
def test_calcular_rechaza_bombas_de_computo(expr):
    out, ok = w("calcular", expresion=expr)
    assert not ok and ("grande" in out or "entre" in out)


@pytest.mark.skipif(not _modulo("sympy"), reason="sin sympy")
def test_algebra():
    assert "[2, 3]" in w("algebra", operacion="resolver", expresion="x^2 - 5x + 6 = 0")[0]
    assert "{x: 2, y: 1}" in w("algebra", operacion="sistema", expresion="2x + y = 5; x - y = 1")[0]
    assert "= 1" in w("algebra", operacion="integrar", expresion="x*e^x", limites=["0", "1"])[0]
    assert "determinante = 5" in w("algebra", operacion="matriz", expresion="2 1; 1 3")[0]
    assert "cos(t)" in w("algebra", operacion="edo", expresion="y'' + y = 0", variable="t",
                         condiciones="y(0)=1; y'(0)=0")[0]


@pytest.mark.skipif(not _modulo("sympy"), reason="sin sympy")
@pytest.mark.parametrize("expr", ["x.__class__", "__import__('os')", "exec('1')", "x; import os", "'a'*3",
                                  "getattr(x, 'y')", "x[0]"])
def test_algebra_filtra_entrada_antes_de_sympy(expr):
    out, ok = w("algebra", operacion="simplificar", expresion=expr)
    assert not ok and "no permitidos" in out


def test_numerico_y_estadistica():
    out, ok = w("numerico", operacion="raiz", expresion="cos(x) - x", intervalo=[0, 1])
    assert ok and "0.7390851332151" in out and "bisección" in out
    out, _ = w("numerico", operacion="raiz", expresion="x^2 + 1", intervalo=[0, 1])
    assert "mismo signo" in out
    out, ok = w("estadistica", datos=[2, 4, 4, 4, 5, 5, 7, 9])
    assert ok and "media = 5" in out and "mediana = 4.5" in out


def test_bits_fechas_y_huella():
    out, _ = w("bits", valor="0xFF", ancho=8)
    assert "con signo (complemento a dos) = -1" in out
    out, _ = w("bits", operacion="ieee754", valor="0.1", tipo="float32")
    assert "0x3DCCCCCD" in out and "13421773/134217728" in out
    out, _ = w("fechas", operacion="diferencia", desde="2026-11-12", hasta="2026-12-25")
    assert out.startswith("43 días") and "laborables (lun-vie, sin festivos) = 31" in out
    out, _ = w("fechas", operacion="sumar", desde="2026-01-31", duracion="1 mes")
    assert out.startswith("2026-02-28")          # fin de mes, no un 31 de febrero
    out, _ = w("fechas", operacion="zona", desde="2026-11-12 18:00", zona_origen="America/Montevideo",
               zona_destino="Europe/Madrid")
    assert "22:00" in out
    assert "b221d9dbb083a7f33428d7c2a3c3198ae925614d70210e28716ccaa7cd4ddb79" in w("huella", texto="hola")[0]


@pytest.mark.skipif(not _modulo("pint"), reason="sin pint")
def test_unidades_y_dimensiones():
    assert "220 Ω" in w("unidades", expresion="11 V / (0.05 A)", a="ohm")[0]
    assert "no cuadran" in w("unidades", expresion="5 kg * 3 m", a="N")[0]


def test_sin_libreria_lo_dice_claro(monkeypatch):
    out, ok = w("logica", smtlib="(declare-const x Int)") if not _modulo("z3") else ("python3-z3", False)
    assert "python3-z3" in out or ok


def test_registradas_en_el_kit_de_calculo():
    assert exactas.NOMBRES <= tools.TOOL_NAMES
    assert exactas.NOMBRES <= kits.KITS["calculo"]["tools"]
    assert "calculo" in kits.from_text("¿cuántos días hay entre el 3 de marzo y hoy?")
    assert "calculo" in kits.from_text("pasa 0x3F a binario con complemento a dos")


@pytest.mark.skipif(not _bwrap_ok(), reason="sin bubblewrap utilizable")
def test_en_la_jaula_sin_red_y_solo_lectura(tmp_path):
    (tmp_path / "datos.csv").write_text("t;v\n1;2,5\n2;3,5\n")
    out, ok = exactas.run(tmp_path, "estadistica", {"archivo": "datos.csv", "columna": "v"})
    assert ok and "media = 3" in out, out
    out, ok = exactas.run(tmp_path, "calcular", {"expresion": "2^10 == 1024"})
    assert ok and "verdadero" in out
    out, ok = exactas.run(tmp_path, "huella", {"archivo": "../../etc/passwd"})
    assert not ok and "dentro del proyecto" in out


def test_sin_bubblewrap_no_ejecuta(tmp_path, monkeypatch):
    monkeypatch.setattr(exactas.shutil, "which", lambda n: None)
    with pytest.raises(exactas.ExactaError):
        exactas.run(tmp_path, "calcular", {"expresion": "1+1"})
