"""Con las API reales de Win32 (solo en Windows; en la CI, el trabajo windows-latest): Job Objects, puertos y el
trabajador de cálculo. La lógica de las reglas se prueba en cualquier sistema en test_plataforma.py."""
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="usa las API de Win32")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import exactas, plataforma as p  # noqa: E402

PY = sys.executable
LIM = p.Limites(memoria=1 << 30, procesos=1, ui=p.UI_ESTRICTA)


def _py(code, lim=LIM, **kw):
    return p.popen_en_job([PY, "-c", code], lim, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)


def test_memoria_y_nombre():
    assert p.ram_total_mb() > 512
    assert p.nombre_proceso(__import__("os").getpid()).lower().startswith("python")


def test_el_proceso_corre_dentro_del_job_restringido():
    worker = Path(p.__file__).with_name("exactas_worker.py")
    code = (f"import sys; sys.path.insert(0, {str(worker.parent)!r}); import exactas_worker as w; "
            "print('JOB' if w.jail_activo() else 'LIBRE')")
    proc = _py(code)
    out, err = proc.communicate(timeout=60)
    p.cerrar_job(proc)
    assert "JOB" in out, err
    # Fuera del Job (este proceso de pytest) no cuenta como aislado
    sys.path.insert(0, str(worker.parent))
    import exactas_worker
    assert exactas_worker.jail_activo() is False


def test_un_solo_proceso_no_puede_crear_otros():
    code = ("import subprocess, sys\n"
            "try:\n    subprocess.run([sys.executable, '-c', 'pass'], timeout=20); print('CREO')\n"
            "except OSError:\n    print('BLOQUEADO')\n")
    proc = _py(code)
    out, _ = proc.communicate(timeout=60)
    p.cerrar_job(proc)
    assert "BLOQUEADO" in out and "CREO" not in out


def test_cerrar_el_job_mata_el_proceso():
    proc = _py("import time; time.sleep(120)")
    time.sleep(1)
    assert proc.poll() is None
    p.cerrar_job(proc)
    proc.wait(10)
    assert proc.returncode is not None


def test_tiempo_agotado_mata_todo_el_arbol():
    lim = p.Limites(procesos=8)
    code = ("import subprocess, sys, time\n"
            "h = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "print(h.pid, flush=True); time.sleep(120)\n")
    nieto = []
    proc = p.popen_en_job([PY, "-c", code], lim, stdout=subprocess.PIPE, text=True)
    nieto.append(int(proc.stdout.readline()))
    p.cerrar_job(proc)
    proc.wait(10)
    for _ in range(50):
        if not p.proceso_vivo(nieto[0]):
            break
        time.sleep(0.1)
    assert not p.proceso_vivo(nieto[0])
    with pytest.raises(subprocess.TimeoutExpired):
        p.ejecutar_en_job([PY, "-c", "import time; time.sleep(60)"], lim, timeout=2)


def test_puerto_propio_y_ajeno():
    code = ("import socket, time\ns = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
            "print(s.getsockname()[1], flush=True); time.sleep(60)\n")
    proc = _py(code)
    try:
        port = int(proc.stdout.readline())
        assert p.puerto_escucha(port)
        assert any(pid == proc.pid for _a, pid in p.escuchando(port))
        assert p.puerto_es_mio(port)
        ajeno = socket.socket()                         # este proceso no lo lanzó TISMA Code
        ajeno.bind(("127.0.0.1", 0))
        ajeno.listen()
        try:
            assert p.puerto_escucha(ajeno.getsockname()[1])
            assert not p.puerto_es_mio(ajeno.getsockname()[1])
        finally:
            ajeno.close()
    finally:
        p.cerrar_job(proc)


def test_motor_se_cierra(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "DIR_DATOS", tmp_path)
    proc = p.lanzar_motor([PY, "-c", "import time; time.sleep(120)"])
    assert proc.pid in p.propios_vivos()
    t0 = time.monotonic()
    p.cerrar_motor(proc, espera=5)
    assert proc.poll() is not None and time.monotonic() - t0 < 30
    assert proc.pid not in p.propios_vivos()


def test_calculo_exacto_aislado(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "DIR_DATOS", tmp_path / "datos")
    out, ok = exactas.run(tmp_path, "calcular", {"expresion": "1/3 + 1/6"})
    assert ok and "= 1/2" in out, out


def test_comando_en_cmd(tmp_path):
    r = p.ejecutar_comando("echo hola& echo adios 1>&2", tmp_path, 30)
    assert r.returncode == 0 and "hola" in r.stdout and "adios" in r.stderr
