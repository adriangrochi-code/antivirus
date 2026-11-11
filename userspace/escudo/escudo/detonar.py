"""Detonación: ejecutar un programa desconocido en un entorno controlado y observar qué intenta hacer.

El programa corre con la identidad del usuario, sin red, con la carpeta personal sustituida por una falsa
llena de SEÑUELOS (clave SSH, perfil de navegador, cartera, documentos) y bajo strace, que registra cada
intento de leer, escribir, conectarse o ejecutar algo. Dos entornos:

* máquina virtual (si hay KVM y la imagen de análisis de TISMA): aislamiento de hardware completo; el
  programa corre en un sistema TISMA mínimo que se apaga al terminar y se descarta;
* sandbox (bubblewrap): sin red, con /home, /tmp y /run vacíos, sin capacidades. Más rápido.

Veredicto: «malicioso» (cuarentena), «sospechoso» (decide el usuario) o «limpio» (su huella SHA-256 pasa a
la lista de permitidos y desde entonces se ejecuta normalmente en el sistema).
Límite honesto: solo se ve lo que el programa hace en ese tiempo; uno que espere horas o que detecte el
análisis puede no mostrar nada.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ALLOWLIST = Path(os.environ.get("ESCUDO_PERMITIDOS", "/var/lib/escudo/primera-ejecucion.json"))
VM_DIR = Path(os.environ.get("ESCUDO_VM_DIR", "/usr/lib/tisma/vm"))
HOME_FALSO = "/home/usuario"
SECRET_DECOYS = {
    ".ssh/id_ed25519": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA-SENUELO-TISMA\n"
                       "-----END OPENSSH PRIVATE KEY-----\n",
    ".aws/credentials": "[default]\naws_access_key_id = AKIASENUELOTISMA0000\naws_secret_access_key = senuelo\n",
    ".config/chromium/Default/Login Data": "SQLite format 3\x00senuelo",
    ".config/chromium/Default/Cookies": "SQLite format 3\x00senuelo",
    ".mozilla/firefox/perfil.default/logins.json": '{"logins": [], "senuelo": true}',
    ".electrum/wallets/default_wallet": '{"senuelo": "tisma"}',
    ".local/share/keyrings/login.keyring": "senuelo",
}
DOC_DECOYS = {f"Documentos/{n}": f"Documento señuelo {n}: " + "texto de prueba " * 200
              for n in ("presupuesto.docx", "fotos-vacaciones.txt", "contrato.pdf", "notas.odt", "tesis.tex")}
PERSISTENCE = re.compile(r"/(\.config/autostart/|\.config/systemd/user/|\.local/share/systemd/user/|\.bashrc$|"
                         r"\.profile$|\.bash_profile$|\.zshrc$|\.pam_environment$|\.xprofile$|\.config/environment\.d/)")
MINER_PORTS = {3333, 4444, 5555, 7777, 14444, 45700}
WRITE_FLAGS = re.compile(r"O_WRONLY|O_RDWR|O_CREAT|O_TRUNC")
STRACE_SET = ("execve,execveat,connect,open,openat,creat,rename,renameat,renameat2,unlink,unlinkat,ptrace,"
              "kill,tgkill,memfd_create,mount,setuid,setgid")


@dataclass
class Informe:
    veredicto: str = "limpio"                  # limpio | sospechoso | malicioso
    motivos: list[str] = field(default_factory=list)
    conexiones: list[str] = field(default_factory=list)
    ejecuciones: list[str] = field(default_factory=list)
    entorno: str = "sandbox"
    segundos: float = 0.0
    sha256: str = ""
    nota: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- análisis de la traza
LINE = re.compile(r'^(?:\[pid\s+)?(\d+)\]?\s+(\w+)\((.*)\)\s+=\s+(-?\w+)')


def analizar_traza(traza: str, home: str = HOME_FALSO, muestra: str = "") -> Informe:
    inf = Informe()
    high, medium = [], []
    secret_paths = {f"{home}/{p}" for p in SECRET_DECOYS}
    doc_paths = {f"{home}/{p}" for p in DOC_DECOYS}
    docs_touched, created = set(), set()
    started = not muestra            # lo anterior a ejecutar la muestra es la propia jaula (bwrap): no cuenta
    for raw in traza.splitlines():
        m = LINE.match(raw.strip())
        if not m:
            continue
        _pid, call, args, _ret = m.groups()
        paths = re.findall(r'"((?:[^"\\]|\\.)*)"', args)
        first = paths[0] if paths else ""
        if not started:
            started = call in ("execve", "execveat") and first == muestra
            continue
        if call in ("open", "openat", "creat"):
            if first in secret_paths:
                high.append(f"intentó leer un secreto (señuelo): {first.replace(home, '~')}")
            writes = call == "creat" or WRITE_FLAGS.search(args)
            if writes and PERSISTENCE.search(first):
                high.append(f"intentó quedarse en el arranque: {first.replace(home, '~')}")
            if writes and first in doc_paths:
                docs_touched.add(first)
            if writes and (first.startswith("/tmp/") or first.startswith(home)):
                created.add(first)
        elif call in ("rename", "renameat", "renameat2", "unlink", "unlinkat"):
            for p in paths:
                if p in doc_paths:
                    docs_touched.add(p)
            if muestra and muestra in paths and call.startswith("unlink"):
                medium.append("borró su propio archivo (borrado de huellas)")
        elif call in ("execve", "execveat"):
            target = first
            if target and target != muestra:
                inf.ejecuciones.append(target)
            if target in created:
                high.append(f"descargó o creó un programa y lo ejecutó: {target.replace(home, '~')}")
            if call == "execveat" and "AT_EMPTY_PATH" in args:
                high.append("ejecutó código desde memoria (sin archivo: técnica para esquivar antivirus)")
            if os.path.basename(target) in ("sudo", "pkexec", "su", "doas"):
                medium.append(f"intentó obtener privilegios de administrador ({os.path.basename(target)})")
            if os.path.basename(target) in ("crontab", "at", "systemctl"):
                medium.append(f"intentó programar tareas o servicios ({os.path.basename(target)})")
        elif call == "connect":
            ip = re.search(r'inet_addr\("([\d.]+)"\)|inet_pton\(AF_INET6, "([0-9a-f:]+)"', args)
            port = re.search(r"htons\((\d+)\)", args)
            if ip and port:
                dest = f"{ip.group(1) or ip.group(2)}:{port.group(1)}"
                if not dest.startswith(("127.", "::1")) and dest not in inf.conexiones:
                    inf.conexiones.append(dest)
                if int(port.group(1)) in MINER_PORTS:
                    high.append(f"conexión a un puerto típico de minería de criptomonedas ({dest})")
        elif call == "ptrace" and "PTRACE_ATTACH" in args or call == "ptrace" and "PTRACE_SEIZE" in args:
            high.append("intentó inyectarse en otro proceso (ptrace)")
        elif call in ("kill", "tgkill") and re.match(r"-1\b", args):
            medium.append("intentó enviar señales a todos los procesos")
        elif call == "mount":
            medium.append("intentó montar sistemas de archivos")
    if len(docs_touched) >= 3:
        high.append(f"modificó, renombró o borró {len(docs_touched)} documentos (comportamiento de ransomware)")
    elif docs_touched:
        medium.append(f"modificó {len(docs_touched)} documento(s) del usuario")
    if len(inf.conexiones) >= 20:
        medium.append(f"intentó conectar con {len(inf.conexiones)} direcciones distintas (escaneo o red de bots)")
    inf.motivos = list(dict.fromkeys(high + medium))
    inf.veredicto = "malicioso" if high else "sospechoso" if medium else "limpio"
    return inf


# ---------------------------------------------------------------- entornos de ejecución
def preparar_home(base: Path) -> Path:
    home = base / "home"
    for rel, content in {**SECRET_DECOYS, **DOC_DECOYS}.items():
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for d in (".config/autostart", ".config/systemd/user", "Descargas"):
        (home / d).mkdir(parents=True, exist_ok=True)
    return home


def sandbox_argv(home: Path, workdir: Path, muestra: str, args: list[str], traza: Path) -> list[str]:
    strace = shutil.which("strace") or "strace"
    bwrap = shutil.which("bwrap") or "bwrap"
    return [strace, "-f", "-qq", "-s", "256", "-e", f"trace={STRACE_SET}", "-o", str(traza), "--",
            bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/var/tmp", "--tmpfs", "/home",
            "--tmpfs", "/media", "--tmpfs", "/mnt",
            "--bind", str(home), HOME_FALSO, "--bind", str(workdir), "/tmp/muestra",
            "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv",
            "--setenv", "HOME", HOME_FALSO, "--setenv", "USER", "usuario", "--setenv", "LANG", "es_ES.UTF-8",
            "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin", "--chdir", HOME_FALSO,
            muestra, *args]


def vm_disponible() -> bool:
    return (os.access("/dev/kvm", os.R_OK | os.W_OK) and shutil.which("qemu-system-x86_64") is not None
            and all((VM_DIR / f).exists() for f in ("vmlinuz", "initrd.img", "detonacion.iso")))


def vm_argv(workdir: Path, segundos: int, kvm: bool = True) -> list[str]:
    append = (f"boot=live components quiet console=ttyS0 tisma.detonar={segundos} "
              "lsm=landlock,lockdown,yama,apparmor,bpf lockdown=integrity panic=5")
    accel = ["-enable-kvm", "-cpu", "host"] if kvm else []        # sin KVM (pruebas): emulado, muy lento
    return ["qemu-system-x86_64", *accel, "-m", "1536", "-smp", "2", "-nographic",
            "-no-reboot", "-nic", "none",             # sin red: lo que intente se registra, no sale
            "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
            "-kernel", str(VM_DIR / "vmlinuz"), "-initrd", str(VM_DIR / "initrd.img"), "-append", append,
            "-drive", f"file={VM_DIR / 'detonacion.iso'},media=cdrom,readonly=on",
            "-virtfs", f"local,path={workdir},mount_tag=muestra,security_model=mapped-xattr,id=muestra"]


def detonar(path: str, args: list[str] | None = None, segundos: int = 45, uid: int | None = None,
            gid: int | None = None, usar_vm: bool | None = None, run=subprocess.run,
            base_dir: Path | None = None) -> Informe:
    """Ejecuta `path` en el entorno controlado y devuelve el informe. Nunca lo ejecuta fuera de él."""
    args = args or []
    digest = sha256_file(path)
    # Fuera de /tmp: la puerta de primera ejecución vigila /tmp y frenaría la propia muestra
    base = Path(tempfile.mkdtemp(prefix="escudo-detonar-", dir=base_dir))
    t0 = time.time()
    try:
        workdir = base / "muestra"
        workdir.mkdir()
        name = re.sub(r"[^\w.+-]", "_", os.path.basename(path))[:80] or "muestra"
        shutil.copyfile(path, workdir / name)
        os.chmod(workdir / name, 0o755)
        home = preparar_home(base)
        # La traza queda FUERA de lo que ve el programa (si no, podría reescribirla para ocultarse)
        traza = base / "traza.txt"
        vm = vm_disponible() if usar_vm is None else usar_vm
        if vm:
            (base / "orden.json").write_text(json.dumps({"muestra": name, "args": args}))
            cmd = vm_argv(base, segundos, kvm=os.access("/dev/kvm", os.W_OK))
            entorno = "máquina virtual"
        else:
            cmd = sandbox_argv(home, workdir, f"/tmp/muestra/{name}", args, traza)
            if uid is not None:                      # con la identidad del usuario, nunca como root
                for p in base.rglob("*"):
                    os.lchown(p, uid, gid if gid is not None else uid)
                os.chown(base, uid, gid if gid is not None else uid)
                cmd = ["setpriv", f"--reuid={uid}", f"--regid={gid if gid is not None else uid}",
                       "--clear-groups", "--", *cmd]
            entorno = "sandbox"
        try:
            run(cmd, capture_output=True, timeout=segundos + (600 if vm else 5), start_new_session=True)
        except subprocess.TimeoutExpired:
            pass                                     # el tiempo de observación terminó: es lo normal
        texto = traza.read_text(errors="replace") if traza.exists() else ""
        inf = analizar_traza(texto, HOME_FALSO, f"/tmp/muestra/{name}")
        inf.entorno, inf.sha256, inf.segundos = entorno, digest, round(time.time() - t0, 1)
        if not texto:
            inf.nota = "no se pudo observar la ejecución (sin traza)"
            inf.veredicto = "sospechoso" if inf.veredicto == "limpio" else inf.veredicto
        return inf
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------- lista de permitidos
def cargar_permitidos(path: Path = ALLOWLIST) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def guardar_veredicto(inf: Informe, ruta: str, path: Path = ALLOWLIST) -> None:
    data = cargar_permitidos(path)
    data[inf.sha256] = {"veredicto": inf.veredicto, "ruta": ruta, "t": time.time(), "motivos": inf.motivos[:5]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


# ---------------------------------------------------------------- dentro de la máquina virtual
def invitado(share: str = "/mnt/muestra", segundos: int = 45) -> None:
    """Lo ejecuta el servicio tisma-detonar dentro de la VM: misma jaula que en el anfitrión (doble barrera)."""
    orden = json.loads(Path(share, "orden.json").read_text())
    name = os.path.basename(orden["muestra"])
    base = Path(tempfile.mkdtemp(prefix="det-"))
    home = preparar_home(base)
    for p in [base, *base.rglob("*")]:
        os.lchown(p, 65534, 65534)                  # dentro de la VM tampoco como root
    cmd = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--",
           *sandbox_argv(home, Path(share, "muestra"), f"/tmp/muestra/{name}", orden.get("args", []),
                         Path(share, "traza.txt"))]
    try:
        subprocess.run(cmd, timeout=segundos, start_new_session=True, capture_output=True)
    except subprocess.TimeoutExpired:
        pass


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "--invitado":
        invitado(sys.argv[2] if len(sys.argv) > 2 else "/mnt/muestra",
                 int(sys.argv[3]) if len(sys.argv) > 3 else 45)
