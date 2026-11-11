"""USB bloqueado con la pantalla bloqueada y reinicio automático (inspirado en GrapheneOS)."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ["TISMA_TEST"] = "1"
loader = importlib.machinery.SourceFileLoader("bloqueo", str(ROOT / "rootfs/usr/lib/tisma/bloqueo-sesion"))
spec = importlib.util.spec_from_loader("bloqueo", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)


class World:
    def __init__(self):
        self.locked, self.sessions, self.calls, self.dpkg = "no", True, [], False

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["loginctl", "list-sessions"]:
            return "2 1000 ana seat0 tty2\n" if self.sessions else ""
        if argv[:2] == ["loginctl", "show-session"]:
            return f"Type=wayland\nClass=user\nLockedHint={self.locked}\nActive=yes\n"
        if argv[:2] == ["systemctl", "is-active"]:
            return "active\n"
        if argv[0] == "pgrep":
            return "123\n" if self.dpkg else ""
        return ""


def test_usb_blocked_only_while_locked():
    w, t = World(), [0.0]
    g = mod.Guard(run=w, now=lambda: t[0], hours=lambda: 0)
    assert g.step() is None                                   # desbloqueada desde el inicio
    w.locked = "yes"
    assert g.step() == "bloqueada"
    assert ["usbguard", "set-parameter", "InsertedDevicePolicy", "block"] in w.calls
    w.locked = "no"
    assert g.step() == "desbloqueada"
    assert w.calls[-1] == ["usbguard", "set-parameter", "InsertedDevicePolicy", "apply-policy"]


def test_auto_reboot_after_hours_but_not_before_first_unlock_or_during_updates():
    w, t = World(), [0.0]
    w.locked = "yes"
    g = mod.Guard(run=w, now=lambda: t[0], hours=lambda: 18)
    g.step()
    t[0] = 30 * 3600
    assert g.step() is None                                   # nunca desbloqueada (ya está «en frío»)
    w.locked = "no"
    g.step()
    w.locked = "yes"
    g.step()
    t[0] += 17 * 3600
    assert g.step() is None
    t[0] += 2 * 3600
    w.dpkg = True
    assert g.step() is None and ["systemctl", "reboot"] not in w.calls
    w.dpkg = False
    assert g.step() == "reinicio" and ["systemctl", "reboot"] in w.calls


def test_config_and_shipped_files(tmp_path):
    f = tmp_path / "r.conf"
    f.write_text("HORAS=500\n")
    assert mod.hours_conf(str(f)) == 72.0
    assert mod.hours_conf(str(tmp_path / "no")) == 0.0
    assert "HORAS=0" in (ROOT / "rootfs/etc/tisma/reinicio-auto.conf").read_text()       # desactivado por defecto
    nm = (ROOT / "rootfs/etc/NetworkManager/conf.d/60-tisma-privacidad.conf").read_text()
    assert "wifi.cloned-mac-address=random" in nm and "dhcp-send-hostname=false" in nm
    pol = json.loads((ROOT / "rootfs/etc/chromium/policies/managed/tisma.json").read_text())
    assert pol["DefaultJavaScriptOptimizerSetting"] == 2
    chrony = (ROOT / "rootfs/etc/chrony/chrony.conf").read_text()
    servers = [l for l in chrony.splitlines() if l.startswith(("server", "pool"))]
    assert servers and all(l.endswith(" nts") for l in servers) and "minsources 2" in chrony
    for svc in ("ssh", "NetworkManager", "polkit"):
        d = (ROOT / f"rootfs/etc/systemd/system/{svc}.service.d/60-tisma-hardened-malloc.conf").read_text()
        assert "LD_PRELOAD=/usr/lib/libhardened_malloc-light.so" in d
