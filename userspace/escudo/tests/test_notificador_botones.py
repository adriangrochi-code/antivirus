"""Avisos de Escudo con botones (sin terminal) y el aviso del kernel solo cuando de verdad hay algo que hacer."""
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "userspace/escudo"))
from escudo import behavior  # noqa: E402


def cargar():
    ruta = str(ROOT / "rootfs/usr/bin/escudo-notificador")
    loader = importlib.machinery.SourceFileLoader("notificador", ruta)
    spec = importlib.util.spec_from_loader("notificador", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class R:
    def __init__(self, stdout="", rc=0):
        self.stdout, self.returncode = stdout, rc


def test_sospechoso_con_boton_permitir(tmp_path):
    n = cargar()
    exe = tmp_path / "programa"
    exe.write_bytes(b"x")
    llamadas = []
    pulsado = lambda argv, **k: llamadas.append(argv) or R("permitir" if "--wait" in argv else "")  # noqa: E731
    alerta = behavior.Alert("primera_ejecucion", "medium", "x: sospechoso", None, str(exe), "Pulsa «Permitir».",
                            boton="permitir")
    t = n.notify(json.dumps(behavior.asdict(alerta)), run=pulsado)
    t.join(5)
    assert "--action" in llamadas[0] and "permitir=Permitir" in llamadas[0]
    assert llamadas[1] == ["pkexec", "/usr/bin/escudo", "permitir", str(exe)]
    assert "ya puedes abrirlo" in llamadas[2][-1]
    assert "sudo" not in json.dumps(behavior.asdict(alerta))


def test_sin_pulsar_no_hace_nada_y_acciones_solo_de_la_lista(tmp_path):
    n = cargar()
    llamadas = []
    n.notify(json.dumps({"kind": "x", "severity": "medium", "detail": "d", "exe": "/bin/sh", "boton": "permitir"}),
             run=lambda argv, **k: llamadas.append(argv) or R("")).join(5)
    assert len(llamadas) == 1                                   # cerrada sin pulsar: ninguna acción
    llamadas.clear()
    assert n.notify(json.dumps({"kind": "x", "severity": "low", "detail": "d", "boton": "rm -rf /"}),
                    run=lambda argv, **k: llamadas.append(argv) or R()) is None
    assert llamadas and "--action" not in llamadas[0]           # acción desconocida: aviso normal sin botón


def test_permitir_solo_archivos_que_existen(tmp_path):
    n = cargar()
    assert n._permitir({"exe": "relativo/x"}) is None and n._permitir({"exe": str(tmp_path / "no")}) is None


def test_aviso_del_kernel_no_salta_con_el_kernel_de_debian(tmp_path):
    script = (ROOT / "rootfs/usr/lib/tisma/kernel-estado").read_text()
    assert "*centinela*" in script and "sudo" not in script and '"boton":"actualizar_kernel"' in script
    falso = tmp_path / "uname"
    falso.write_text("#!/bin/sh\necho 6.12.48+deb13-amd64\n")
    falso.chmod(0o755)
    r = subprocess.run(["sh", str(ROOT / "rootfs/usr/lib/tisma/kernel-estado")], capture_output=True, text=True,
                       env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert r.returncode == 0 and not r.stdout


def test_32_bits_sin_ordenes_si_falla():
    texto = (ROOT / "rootfs/usr/bin/tisma-perfil").read_text()
    assert "sudo update-grub" not in texto
