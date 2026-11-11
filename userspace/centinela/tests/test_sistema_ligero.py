"""Configuración del sistema que no debe romperse sin querer: mantenimiento con el equipo libre, inicio de
sesión liviano y la VM de análisis que se apaga sola."""
import configparser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / "rootfs"
SD = ROOT / "etc/systemd/system"
PESADAS = ["tisma-copias-externo", "tisma-drivers", "tisma-salud", "tisma-scrub", "chkrootkit", "lynis",
           "e2scrub_all", "fstrim", "tisma-flatpak-actualizar"]


def ini(path: Path) -> configparser.RawConfigParser:
    c = configparser.RawConfigParser(strict=False, interpolation=None)
    c.optionxform = str
    c.read(path, encoding="utf-8")
    return c


def test_heavy_jobs_wait_for_a_free_machine_with_lowest_priority():
    for u in PESADAS:
        s = ini(SD / f"{u}.service.d/60-tisma-equipo-libre.conf")["Service"]
        assert s["ExecStartPre"] == "/usr/bin/centinela esperar-libre", u
        assert s["TimeoutStartSec"] == "infinity" and s["IOSchedulingClass"] == "idle" and s["CPUWeight"] == "idle"
    # esperar en vez de saltar: una condición de batería saltaría el scrub un mes entero
    assert "ConditionACPower" not in (SD / "tisma-scrub.service").read_text()


def test_login_is_lighter():
    a = ROOT / "etc/xdg/autostart"
    assert ini(a / "org.kde.discover.notifier.desktop")["Desktop Entry"]["Hidden"] == "true"
    assert ini(a / "org.kde.kdeconnect.daemon.desktop")["Desktop Entry"]["X-KDE-autostart-phase"] == "2"
    assert (SD / "tisma-flatpak-actualizar.timer").exists()        # lo que el notificador no hacía: actualizar


def test_analysis_vm_powers_off_when_done():
    c = ini(SD / "tisma-detonar.service")
    assert c["Unit"]["SuccessAction"] == "poweroff-force" and c["Unit"]["FailureAction"] == "poweroff-force"
    assert "SuccessAction" not in c["Service"]
