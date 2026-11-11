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


SEGURIDAD = {"chkrootkit", "lynis", "tisma-drivers", "tisma-flatpak-actualizar"}


def test_heavy_jobs_wait_for_a_free_machine_with_lowest_priority():
    for u in PESADAS:
        s = ini(SD / f"{u}.service.d/60-tisma-equipo-libre.conf")["Service"]
        esperado = "/usr/bin/centinela esperar-libre" + (" --seguridad" if u in SEGURIDAD else "")
        assert s["ExecStartPre"] == esperado, u                    # la seguridad nunca se salta
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


def test_escudo_protects_before_any_login():
    c = ini(SD / "escudo.service")
    u, sv = c["Unit"], c["Service"]
    assert "display-manager.service" in u["Before"] and "systemd-user-sessions.service" in u["Before"]
    assert "network-online.target" not in u.get("After", "")          # sin red no bloquea el inicio de sesión
    assert sv["Type"] == "notify" and c["Install"]["WantedBy"] == "multi-user.target"
    assert "Requires" not in u                                        # si fallara, no deja el equipo inusable
    fase2 = (SD / "tisma-fase2.target.d/servicios.conf").read_text()
    assert "escudo.service" not in fase2.split("[Unit]", 1)[1]


def test_group_a_hardening_is_in_place():
    cmd = (ROOT / "etc/default/grub.d/50-centinela.cfg").read_text()
    for p in ("intel_iommu=on", "amd_iommu=force_isolation", "iommu.passthrough=0", "random.trust_cpu=off",
              "random.trust_bootloader=off", "efi_pstore.pstore_disable=1", "erst_disable",
              "proc_mem.force_override=ptrace", "kfence.sample_interval=100"):
        assert p in cmd, p
    assert "extra_latent_entropy" not in cmd                      # no existe en el kernel oficial
    sysctl = (ROOT / "etc/sysctl.d/50-centinela-hardening.conf").read_text()
    assert "kernel.core_pattern = |/bin/false" in sysctl and "net.ipv4.conf.all.arp_ignore = 2" in sysctl
    assert 'APT::Sandbox::Seccomp "true"' in (ROOT / "etc/apt/apt.conf.d/40-tisma-sandbox").read_text()
    fl = (ROOT / "etc/security/faillock.conf").read_text()
    assert "deny = 10" in fl and "unlock_time = 600" in fl
    pc = ROOT / "usr/share/pam-configs"
    assert "pam_faillock.so preauth" in (pc / "tisma-faillock-pre").read_text()
    assert "[default=die]\tpam_faillock.so authfail" in (pc / "tisma-faillock-fallo").read_text()
    assert "Priority: 128" in (pc / "tisma-faillock-fallo").read_text()       # después de pam_unix (256)
    assert "pam_faillock.so authsucc" in (pc / "tisma-faillock-ok").read_text()
    assert ini(SD / "tisma-montajes-seguros.service")["Service"]["ExecStart"] == "/usr/lib/tisma/montajes-seguros"
