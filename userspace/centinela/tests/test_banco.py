"""Banco de pruebas (sección 8): repeticiones, energía de la batería, CSV y tabla comparativa."""
import csv

from centinela import banco


def test_corrida_csv_y_tabla(tmp_path, monkeypatch):
    monkeypatch.setattr(banco, "temperatura", lambda: 50.0)
    monkeypatch.setattr(banco, "bateria", lambda: (8.0, True))
    monkeypatch.setattr(banco, "psi", lambda r: 0.5)
    valores = iter([10.0, 12.0, 8.0, 9.0])
    import functools
    import time
    monkeypatch.setattr(banco, "Medidor", functools.partial(banco.Medidor, paso=0.01))

    def falsa(**_):
        time.sleep(0.05)
        return {"tiempo": (next(valores), "s")}
    monkeypatch.setitem(banco.TAREAS, "falsa", falsa)
    a = banco.correr(["falsa"], "debian", 2, str(tmp_path / "a.csv"), progreso=lambda m: None)
    b = banco.correr(["falsa"], "tisma-eco", 2, str(tmp_path / "b.csv"), progreso=lambda m: None)
    filas = list(csv.DictReader(open(a)))
    assert len(filas) == 2 and set(banco.COLUMNAS) == set(filas[0])
    assert filas[0]["a_bateria"] == "True" and filas[0]["temp_inicio_c"] == "50.0"
    t = banco.tabla([a, b])
    assert "| falsa | — | tiempo (s) | 11 ± 1.4 | 8.5 ± 0.71 (-23 %) |" in t
    assert "julios (J)" in t


def test_sin_bateria_no_hay_vatios(monkeypatch):
    monkeypatch.setattr(banco, "bateria", lambda: (5.0, False))           # enchufado: no se mide
    monkeypatch.setattr(banco, "temperatura", lambda: None)
    with banco.Medidor(paso=0.01) as m:
        import time
        time.sleep(0.05)
    assert m.vatios is None and m.julios is None


def test_inventario_campos():
    d = banco.inventario(run=lambda argv: "")
    for k in ("cpu", "memoria_dmi", "equipo_dmi", "modulos_cmt", "edac", "vulnerabilidades", "tpm", "bateria",
              "cpufreq", "rapl"):
        assert k in d
