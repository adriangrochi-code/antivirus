"""Perfiles de sandbox y construcción de la línea de bubblewrap.

Principio: **todo denegado salvo lo que el perfil concede**.
- Sistema en solo lectura (/usr, /etc); nada del $HOME real salvo las carpetas concedidas.
- Cada app tiene su propio $HOME privado (~/.local/share/tisma-sandbox/<app>): sus
  configuraciones y cachés no se mezclan con las de otras apps ni con tus archivos.
- Sin red, GPU, audio, cámara ni D-Bus salvo concesión explícita.
- D-Bus siempre filtrado por xdg-dbus-proxy (la app solo habla con los servicios listados).
- Espacios de nombres propios (PID, IPC, UTS, cgroup), sin capacidades, nueva sesión (evita TIOCSTI).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

SYSTEM_DIR = Path("/usr/share/tisma-sandbox/perfiles")
USER_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tisma-sandbox"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "tisma-sandbox"
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# Rutas del $HOME que ningún perfil puede conceder (secretos y persistencia)
FORBIDDEN = ["~/.ssh", "~/.gnupg", "~/.config/autostart", "~/.config/systemd", "~/.bashrc", "~/.profile",
             "~/.bash_profile", "~/.zshrc", "~/.local/share/keyrings", "~/.local/bin", "~/.pam_environment",
             "~/.config/tisma-sandbox", "~/.local/share/tisma-sandbox", "~"]
DEVICES = {"gpu": ["/dev/dri", "/dev/nvidia0", "/dev/nvidiactl", "/dev/nvidia-modeset", "/dev/nvidia-uvm"],
           "camara": ["/dev/video0", "/dev/video1", "/dev/media0"],
           "mandos": ["/dev/input", "/dev/uinput", "/dev/hidraw0"]}


class ProfileError(ValueError):
    pass


@dataclass
class Profile:
    nombre: str
    exec: str
    descripcion: str = ""
    red: bool = False
    gpu: bool = False
    audio: bool = False
    camara: bool = False
    mandos: bool = False
    wayland: bool = True
    x11: bool = False
    portales: bool = True                 # selector de archivos del sistema (xdg-desktop-portal)
    rw: list[str] = field(default_factory=list)
    ro: list[str] = field(default_factory=list)
    dbus_talk: list[str] = field(default_factory=list)
    dbus_own: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    escritorio: str | None = None         # ID .desktop a reemplazar en el menú

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ProfileError(f"Campos desconocidos en el perfil: {', '.join(sorted(unknown))}")
        p = cls(**d)
        p.validate()
        return p

    def validate(self) -> None:
        if not _NAME.match(self.nombre):
            raise ProfileError(f"Nombre de perfil inválido: {self.nombre!r}")
        if not os.path.isabs(self.exec) or ".." in Path(self.exec).parts:
            raise ProfileError("exec debe ser una ruta absoluta")
        for p in self.rw + self.ro:
            if not p.startswith(("~/", "/media/", "/mnt/", "/run/media/")) or ".." in Path(p).parts:
                raise ProfileError(f"Ruta no permitida en el perfil: {p}")
            exp = os.path.normpath(os.path.expanduser(p))
            for f in FORBIDDEN:
                fx = os.path.normpath(os.path.expanduser(f))
                if exp == fx or (f != "~" and exp.startswith(fx + os.sep)):
                    raise ProfileError(f"{p}: por seguridad ningún perfil puede acceder a {f}")
        for name in self.dbus_talk + self.dbus_own:
            if not re.match(r"^[A-Za-z0-9_.*-]+$", name) or name in ("*", "org.*", "org.freedesktop.*"):
                raise ProfileError(f"Nombre D-Bus demasiado amplio o inválido: {name}")
        for k in self.env:
            if not re.match(r"^[A-Z_][A-Z0-9_]*$", k) or k in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT"):
                raise ProfileError(f"Variable de entorno no permitida: {k}")


def load(name: str) -> Profile:
    if not _NAME.match(name):
        raise ProfileError(f"Nombre de perfil inválido: {name!r}")
    for d in (USER_DIR, SYSTEM_DIR):
        f = d / f"{name}.json"
        if f.is_file():
            return Profile.from_dict(json.loads(f.read_text(encoding="utf-8")))
    raise ProfileError(f"No existe el perfil «{name}»")


def available() -> list[str]:
    names = set()
    for d in (SYSTEM_DIR, USER_DIR):
        names |= {p.stem for p in d.glob("*.json")} if d.is_dir() else set()
    return sorted(names)


def bwrap_args(p: Profile, *, uid: int, runtime_dir: str, app_home: Path, dbus_socket: str | None,
               exists=os.path.exists) -> list[str]:
    """Argumentos de bubblewrap (sin el ejecutable ni el comando final)."""
    home = str(Path.home())
    a = ["--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
         "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
         "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
         "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/var/tmp",
         "--dir", "/run", "--dir", runtime_dir, "--chmod", "0700", runtime_dir,
         "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try",
         "--die-with-parent", "--new-session", "--cap-drop", "ALL",
         "--hostname", p.nombre,
         # $HOME privado de la app
         "--bind", str(app_home), home, "--setenv", "HOME", home,
         "--setenv", "XDG_RUNTIME_DIR", runtime_dir, "--setenv", "TISMA_SANDBOX", p.nombre]
    if not p.red:
        a += ["--unshare-net"]
    else:
        a += ["--ro-bind-try", "/run/systemd/resolve", "/run/systemd/resolve"]
    for dev_kind in ("gpu", "camara", "mandos"):
        if getattr(p, dev_kind):
            a += [x for d in DEVICES[dev_kind] if exists(d) for x in ("--dev-bind", d, d)]
    if p.gpu:
        a += ["--ro-bind-try", "/sys/dev/char", "/sys/dev/char", "--ro-bind-try", "/sys/devices", "/sys/devices"]
    real_rt = f"/run/user/{uid}"
    if p.wayland:
        wd = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        a += ["--ro-bind-try", f"{real_rt}/{wd}", f"{runtime_dir}/{wd}", "--setenv", "WAYLAND_DISPLAY", wd]
    if p.x11:
        a += ["--ro-bind-try", "/tmp/.X11-unix", "/tmp/.X11-unix",
              "--setenv", "DISPLAY", os.environ.get("DISPLAY", ":0")]
    if p.audio:
        a += ["--ro-bind-try", f"{real_rt}/pipewire-0", f"{runtime_dir}/pipewire-0",
              "--ro-bind-try", f"{real_rt}/pulse/native", f"{runtime_dir}/pulse/native"]
    if dbus_socket:
        a += ["--ro-bind", dbus_socket, f"{runtime_dir}/bus",
              "--setenv", "DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus"]
    for path in p.ro:
        src = os.path.expanduser(path)
        a += ["--ro-bind-try", src, src]
    for path in p.rw:
        src = os.path.expanduser(path)
        if src.startswith(home + os.sep):
            os.makedirs(src, exist_ok=True)
        a += ["--bind-try", src, src]
    for k, v in p.env.items():
        a += ["--setenv", k, v]
    a += ["--unsetenv", "SSH_AUTH_SOCK", "--unsetenv", "GPG_AGENT_INFO", "--unsetenv", "LD_PRELOAD"]
    return a


def dbus_proxy_args(p: Profile) -> list[str]:
    talk = list(p.dbus_talk)
    if p.portales:
        talk.append("org.freedesktop.portal.*")
    args = ["--filter"]
    args += [f"--talk={n}" for n in talk]
    args += [f"--own={n}" for n in p.dbus_own]
    return args


def matches_forbidden(path: str) -> bool:
    exp = os.path.normpath(os.path.expanduser(path))
    return any(fnmatch.fnmatch(exp, os.path.normpath(os.path.expanduser(f))) for f in FORBIDDEN if f != "~")
