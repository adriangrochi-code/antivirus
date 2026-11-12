"""Configuración del sistema que no debe romperse sin querer: mantenimiento con el equipo libre, inicio de
sesión liviano y la VM de análisis que se apaga sola."""
import configparser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / "rootfs"
SD = ROOT / "etc/systemd/system"
PESADAS = ["tisma-copias-externo", "tisma-drivers", "tisma-salud", "tisma-scrub", "chkrootkit", "lynis",
           "e2scrub_all", "fstrim", "tisma-flatpak-actualizar", "escudo-actualizar"]


def ini(path: Path) -> configparser.RawConfigParser:
    c = configparser.RawConfigParser(strict=False, interpolation=None)
    c.optionxform = str
    c.read(path, encoding="utf-8")
    return c


SEGURIDAD = {"chkrootkit", "lynis", "tisma-drivers", "tisma-flatpak-actualizar", "escudo-actualizar"}


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


def test_installer_encrypts_by_default():
    import yaml
    p = yaml.safe_load((ROOT / "etc/calamares/modules/partition.conf").read_text())
    assert p["preCheckEncryption"] is True and p["enableLuksAutomatedPartitioning"] is True
    # /boot aparte SIN cifrar (la contraseña la pide la pantalla de arranque, con el teclado bien) y el resto LUKS2
    assert p["luksGeneration"] == "luks2"
    capas = {c["mountPoint"]: c for c in p["partitionLayout"]}
    assert capas["/boot"]["noEncrypt"] is True and capas["/boot"]["filesystem"] == "ext4"
    assert not capas["/"].get("noEncrypt") and capas["/"]["filesystem"] == "btrfs"
    # Como Windows: borrar el disco preseleccionado, sin particionado manual
    assert p["initialPartitioningChoice"] == "erase" and p["allowManualPartitioning"] is False
    # Sin cryptsetup-initramfs un sistema cifrado no abre el disco al arrancar (faltaba en la preview 5)
    pk = (ROOT.parent / "iso/packages.list").read_text().split()
    assert "cryptsetup-initramfs" in pk


def test_installer_pages_like_windows():
    import yaml
    st = yaml.safe_load((ROOT / "etc/calamares/settings.conf").read_text())
    show = st["sequence"][0]["show"]
    assert show == ["welcome", "partition", "users", "summary"]     # sin región ni teclado
    ex = st["sequence"][1]["exec"]
    assert "shellprocess@region" in ex and ex.index("shellprocess@region") < ex.index("users")
    assert not {"locale", "keyboard", "localecfg"} & set(ex)
    u = yaml.safe_load((ROOT / "etc/calamares/modules/users.conf").read_text())
    assert u["setRootPassword"] is False and u["doAutologin"] is False
    assert u["passwordRequirements"]["minLength"] >= 8 and u["sudoersGroup"] == "sudo"
    assert yaml.safe_load((ROOT / "etc/calamares/modules/finished.conf").read_text())["restartNowMode"] == "always"
    r = yaml.safe_load((ROOT / "etc/calamares/modules/shellprocess-region.conf").read_text())
    assert r["dontChroot"] is True and any("instalar-region ${ROOT}" in c for c in r["script"])
    # Quitar lo del USB sin fallar por paquetes que TISMA no trae (la instalación fallaba al final)
    pk = yaml.safe_load((ROOT / "etc/calamares/modules/packages.conf").read_text())
    ops = pk["operations"]
    assert all(set(o) == {"try_remove"} for o in ops) and "live-boot" in ops[0]["try_remove"]
    # El icono y el arranque automático usan el lanzador que limpia un intento fallido anterior
    assert "Exec=/usr/lib/tisma/instalar-tisma" in (ROOT / "usr/share/tisma/live/instalar-tisma.desktop").read_text()
    assert "exec /usr/lib/tisma/instalar-tisma" in (ROOT / "usr/lib/tisma/instalador-auto").read_text()


def test_dns_blocks_malware_with_live_resolvers():
    import tomllib
    d = tomllib.loads((ROOT / "etc/dnscrypt-proxy/dnscrypt-proxy.toml").read_text())
    assert all("nofilter" not in s and "mullvad" not in s for s in d["server_names"])   # mullvad-doh: apagado
    assert d["require_nofilter"] is False                          # si no, se excluirían los que filtran
    assert "tif.medium" in (ROOT / "usr/lib/tisma/actualizar-bloqueo").read_text()
    assert "OnUnitActiveSec=1d" in (SD / "tisma-bloqueo.timer").read_text()


def test_iconos_de_apps_distintos_y_existentes():
    """Cada app de TISMA tiene su icono de la familia (no el logo repetido) y el icono existe."""
    import importlib.util
    import re
    spec = importlib.util.spec_from_file_location("iconos", ROOT.parent / "branding/iconos.py")
    ic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ic)
    disponibles = {f"tisma-{n}" for n in ic.APPS} | {"tisma"}
    usados = {}
    for f in list((ROOT / "usr/share/applications").glob("*.desktop")) + [ROOT / "usr/share/tisma/live/instalar-tisma.desktop"]:
        m = re.search(r"^Icon=(.+)$", f.read_text(), re.M)
        if m and m.group(1).startswith("tisma"):
            assert m.group(1) in disponibles, (f.name, m.group(1))
            usados[f.name] = m.group(1)
    assert len(usados) >= 8 and len(set(usados.values())) == len(usados)      # ninguno repetido
    colores = [c for c, _, _ in ic.APPS.values()]
    assert len(set(colores)) == len(colores)
    import xml.dom.minidom
    for n in ic.APPS:
        xml.dom.minidom.parseString(ic.icono(n))                              # SVG válido


def test_pantalla_de_inicio_en_wayland():
    """Sin servidor X en la imagen, sddm con X11 no arrancaba en el sistema instalado (pantalla negra)."""
    import configparser
    c = configparser.ConfigParser()
    c.read(ROOT / "etc/sddm.conf.d/10-tisma-wayland.conf")
    assert c["General"]["DisplayServer"] == "wayland"
    assert c["Wayland"]["CompositorCommand"].startswith("kwin_wayland")
