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
    assert show == ["welcome", "partition", "users", "packagechooserq@perfiles", "summary"]   # sin región ni teclado
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
    auto = (ROOT / "usr/lib/tisma/instalador-auto").read_text()
    assert "exec /usr/lib/tisma/instalar-tisma" in auto and "exec /usr/bin/tisma-actualizar-usb" in auto


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


def test_lanzadores_fijos_existen_siempre():
    """Un lanzador anclado de una app que no está sale como «?» en la barra de tareas."""
    import re
    lay = (ROOT / "usr/share/plasma/look-and-feel/org.centinela.desktop/contents/layouts/"
                  "org.kde.plasma.desktop-layout.js").read_text()
    paquetes = (ROOT.parent / "iso/packages.list").read_text().split()
    for app in re.findall(r'"applications:([^"]+)\.desktop"', lay):
        en_rootfs = (ROOT / "usr/share/applications" / f"{app}.desktop").exists()
        de_paquete = {"org.kde.dolphin": "dolphin", "systemsettings": "kde-plasma-desktop"}.get(app)
        assert en_rootfs or (de_paquete and de_paquete in paquetes), app


def test_atajos_de_windows():
    texto = (ROOT / "etc/skel/.config/kglobalshortcutsrc").read_text(encoding="utf-8")
    # Cada atajo de Windows apunta a lo suyo y no queda duplicado en otra acción (Win+R era grabar la pantalla)
    esperado = {
        "[kwin]": {"Window Maximize": "Meta+Up", "Window Minimize": "Meta+Down", "MinimizeAll": "Meta+M"},
        "[services][org.kde.krunner.desktop]": {"_launch": "Meta+R"},
        "[services][org.kde.spectacle.desktop]": {"RectangularRegionScreenShot": "Meta+Shift+S"},
        "[services][org.kde.plasma-systemmonitor.desktop]": {"_launch": "Ctrl+Shift+Esc"},
        "[services][systemsettings.desktop]": {"_launch": "Meta+I"},
    }
    grupos: dict[str, dict[str, str]] = {}
    actual = None
    for linea in texto.splitlines():
        if linea.startswith("["):
            actual = linea.strip()
            grupos[actual] = {}
        elif "=" in linea and not linea.startswith("#") and actual:
            k, v = linea.split("=", 1)
            grupos[actual][k] = v
    usados: dict[str, str] = {}
    for grupo, acciones in grupos.items():
        for accion, valor in acciones.items():
            activos = valor.split(",")[0] if grupo == "[kwin]" else valor
            for tecla in activos.split("\\t"):
                if tecla != "none":
                    assert tecla not in usados, f"{tecla} en {accion} y en {usados[tecla]}"
                    usados[tecla] = accion
    for grupo, acciones in esperado.items():
        for accion, tecla in acciones.items():
            assert tecla in grupos[grupo][accion].split(",")[0].split("\\t"), (grupo, accion)
    assert "minimizeallEnabled=true" in (ROOT / "etc/xdg/kwinrc").read_text(encoding="utf-8")


def test_menu_nuevo_con_documentos_de_office(tmp_path):
    import importlib.util
    import zipfile
    spec = importlib.util.spec_from_file_location("plantillas", ROOT.parent / "iso/plantillas-nuevo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.escribir(tmp_path)
    base = tmp_path / "usr/share/templates"
    for desktop in base.glob("*.desktop"):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(desktop, encoding="utf-8")
        assert (base / cp["Desktop Entry"]["URL"]).is_file()
    for ext in ("docx", "xlsx", "pptx"):
        (archivo,) = (base / ".source").glob(f"*.{ext}")
        with zipfile.ZipFile(archivo) as z:
            assert z.testzip() is None and "[Content_Types].xml" in z.namelist()
            assert not any(n.endswith((".bin", "vbaProject.bin")) for n in z.namelist())  # sin macros
    assert 'plantillas-nuevo.py' in (ROOT.parent / "iso/build-iso.sh").read_text(encoding="utf-8")


def test_escritorio_como_windows():
    # Al entrar no se reabren las ventanas de la sesión anterior
    assert "loginMode=emptySession" in (ROOT / "etc/xdg/ksmserverrc").read_text(encoding="utf-8")
    # Win+R / Alt+Espacio: KRunner tiene tiempo de arrancar en equipos lentos (Plasma le da 5 s)
    k = (ROOT / "etc/systemd/user/plasma-krunner.service.d/50-tisma-tiempo.conf").read_text(encoding="utf-8")
    assert "TimeoutSec=30" in k
    # El icono del USB va en «Escritorio»: con ~/Desktop, xdg-user-dirs dejaba una carpeta en inglés
    build = (ROOT.parent / "iso/build-iso.sh").read_text(encoding="utf-8")
    assert "/etc/skel/Escritorio/instalar-tisma.desktop" in build and "/etc/skel/Desktop" not in build
    region = (ROOT / "etc/calamares/modules/shellprocess-region.conf").read_text(encoding="utf-8")
    assert "rmdir ${ROOT}/etc/skel/Escritorio" in region


def test_explorador_y_energia_como_windows():
    paquetes = (ROOT.parent / "iso/packages.list").read_text(encoding="utf-8").split()
    # Sin «Recommends» faltaban: energía (brillo, suspender, batería), miniaturas, MTP y la red de Windows
    for p in ("powerdevil", "kio-extras", "ffmpegthumbs", "kdegraphics-thumbnailers", "dolphin-plugins"):
        assert p in paquetes, p
    nft = (ROOT / "etc/nftables.conf").read_text(encoding="utf-8")
    # WS-Discovery solo desde la red local, nunca abierto a cualquiera
    reglas = [l.strip() for l in nft.splitlines() if "3702" in l and not l.strip().startswith("#")]
    assert len(reglas) == 2 and all(r.startswith(("ip saddr {", "ip6 saddr {")) for r in reglas)
    assert "policy drop" in nft
    # Los compiladores no quedan en la imagen
    assert "purge -y -q clang-19 clang-tools-19" in (ROOT.parent / "iso/build-iso.sh").read_text(encoding="utf-8")


def test_compartir_carpeta_seguro():
    smb = (ROOT / "etc/samba/smb.conf").read_text(encoding="utf-8")
    for linea in ("server min protocol = SMB3", "map to guest = never", "usershare allow guests = no",
                  "disable netbios = yes", "usershare owner only = yes", "hosts deny = ALL",
                  "restrict anonymous = 1",
                  "hosts allow = 127.0.0.1 ::1 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 fe80::/10 fc00::/7"):
        assert linea in smb, linea
    # Sin nada compartido Samba solo escucha en el propio equipo (Dolphin lo necesita para crear el recurso: con
    # smbd parado «net usershare add» falla, visto en la VM); tisma-red.conf lo abre a la red. NetBIOS nunca.
    assert "interfaces = lo" in smb and "bind interfaces only = yes" in smb
    glob, plantilla = smb.split("\n[tisma-plantilla]")
    ultima = [l.strip() for l in glob.splitlines() if l.strip() and not l.strip().startswith("#")][-1]
    assert ultima == "include = /etc/samba/tisma-red.conf"   # al final de [global]: lo incluido gana
    # Cifrado solo en lo compartido (en [global] rompe «net usershare add»)
    assert "smb encrypt" not in glob and "smb encrypt = desired" in plantilla and "-valid = no" in plantilla and "path =" in plantilla
    assert "usershare template share = tisma-plantilla" in glob
    assert "bind interfaces only" not in (ROOT / "etc/samba/tisma-red.conf").read_text(encoding="utf-8")
    assert "ConditionPathExists=/etc/tisma/netbios-activado" in (
        ROOT / "etc/systemd/system/nmbd.service.d/60-tisma-sin-netbios.conf").read_text(encoding="utf-8")
    # El 445 solo se abre a la red local, mediante el conjunto que maneja tisma-compartir
    nft = (ROOT / "etc/nftables.conf").read_text(encoding="utf-8")
    reglas = [l.strip() for l in nft.splitlines() if "@compartir" in l and not l.strip().startswith("#")]
    assert len(reglas) == 2 and all(r.startswith(("ip saddr {", "ip6 saddr {")) for r in reglas)
    assert "445" not in "".join(l for l in nft.splitlines() if not l.strip().startswith("#"))
    script = (ROOT / "usr/lib/tisma/compartir-red").read_text(encoding="utf-8")
    assert "nft add element inet centinela compartir '{ 445 }'" in script and "nft flush set" in script
    assert "bind interfaces only = no" in script and "try-restart smbd" in script
    users = (ROOT / "etc/calamares/modules/users.conf").read_text(encoding="utf-8")
    assert "name: sambashare" in users and "must_exist: false" in users
    assert "tisma-compartir.path" in (ROOT.parent / "iso/build-iso.sh").read_text(encoding="utf-8")


def test_instalador_al_frente_en_el_usb():
    import configparser as cp
    reglas = cp.ConfigParser(interpolation=None)
    reglas.read(ROOT / "usr/share/tisma/live/kwinrulesrc", encoding="utf-8")
    r = reglas["tisma-instalador"]
    assert reglas["General"]["rules"] == "tisma-instalador" and reglas["General"]["count"] == "1"
    # Ventana de Calamares: sin prevención de robo de foco (forzado), sin pantalla completa, maximizada
    assert r["wmclass"] == "calamares" and r["wmclassmatch"] == "1"
    assert (r["fsplevel"], r["fsplevelrule"]) == ("0", "2")
    assert (r["fullscreen"], r["fullscreenrule"]) == ("false", "2")
    assert r["maximizehoriz"] == r["maximizevert"] == "true"
    # Solo en la sesión del USB
    assert "/etc/xdg/kwinrulesrc" in (ROOT / "usr/lib/tisma/sesion-live").read_text(encoding="utf-8")
    assert not (ROOT / "etc/xdg/kwinrulesrc").exists()


def test_touchpad_toca_para_clic():
    # KWin 6.3 usa [Libinput][Defaults][Touchpad] de kcminputrc (que lee también de /etc/xdg) para todos los
    # touchpads, incluida la pantalla de inicio de sesión
    t = (ROOT / "etc/xdg/kcminputrc").read_text(encoding="utf-8")
    assert "[Libinput][Defaults][Touchpad]\nTapToClick=true" in t


def test_menu_de_arranque_legible_con_secure_boot():
    import re
    build = (ROOT.parent / "iso/build-iso.sh").read_text(encoding="utf-8")
    # Con Secure Boot GRUB no carga fuentes y la suya no tiene acentos («gr?ficos b?sicos»): opciones sin tildes
    for titulo in re.findall(r'menuentry "([^"]*)"', build):
        assert titulo.isascii(), titulo
    assert 'if [ "\\$lockdown" != "y" ]; then' in build and "for f in /boot/grub/themes" not in build
    tema = (ROOT / "boot/grub/themes/centinela/theme.txt").read_text(encoding="utf-8")
    # Sin rótulo «TISMA»: en la A275 quedaba encima de la caja violeta de la opción elegida
    assert "wordmark" not in tema.split("acentos):", 1)[-1]


def test_bandeja_completa_en_el_primer_inicio():
    # Tocar el contenedor interno de la bandeja desde el script del tema la dejaba vacía (sin Wi-Fi ni volumen)
    js = (ROOT / "usr/share/plasma/look-and-feel/org.centinela.desktop/contents/layouts/"
          "org.kde.plasma.desktop-layout.js").read_text(encoding="utf-8")
    codigo = "\n".join(l for l in js.splitlines() if not l.strip().startswith("//"))
    assert "org.kde.plasma.systemtray" in codigo
    assert "SystrayContainmentId" not in codigo and "extraItems" not in codigo


def test_dependencias_de_hope_solo_con_hope_y_de_debian():
    """Las herramientas exactas de Hope (orden acordado con Hope): de Debian, sin GPL y solo si Hope va en la imagen."""
    lista = (ROOT.parent / "iso/hope-paquetes.list").read_text(encoding="utf-8")
    paq = [p for linea in lista.splitlines() if not linea.lstrip().startswith("#") for p in linea.split()]
    orden = ["python3-mpmath", "python3-highspy", "python3-cvc5", "python3-networkx", "python3-uncertainties",
             "python3-holidays", "python3-periodictable", "python3-fluids", "python3-shapely", "python3-statsmodels"]
    assert [p for p in paq if p in orden] == orden and "python3-z3" in paq and "python3-pulp" in paq
    assert all(p.startswith("python3-") for p in paq)
    build = (ROOT.parent / "iso/build-iso.sh").read_text(encoding="utf-8")
    rama = build[build.index("fetch-hope.sh"):build.index("rm -rf \"$LIB/python/vendor\"")]
    assert "hope-paquetes.list" in rama              # dentro de la rama «Hope va en la imagen»
    base = (ROOT.parent / "iso/packages.list").read_text(encoding="utf-8")
    assert "python3-cvc5" not in base and "python3-statsmodels" not in base   # la ISO pública no los carga


def test_menu_limpio(tmp_path):
    """Pocas categorías y sin lanzadores técnicos o repetidos (Chromium suelto, Discover, cpupower-gui…)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("lanzadores", ROOT.parent / "iso/lanzadores.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    apps = tmp_path / "usr/share/applications"
    apps.mkdir(parents=True)
    (apps / "chromium.desktop").write_text("[Desktop Entry]\nName=Chromium\nNoDisplay=false\n\n[Desktop Action x]\nName=y\n")
    (apps / "com.github.jeromerobert.pdfarranger.desktop").write_text(
        "[Desktop Entry]\nName=PDF Arranger\nName[es]=Ventana nueva\n\n[Desktop Action new]\nName[es]=Ventana nueva\n")
    assert m.aplicar(str(tmp_path)) == ["chromium.desktop", "com.github.jeromerobert.pdfarranger.desktop"]
    local = tmp_path / "usr/local/share/applications"
    c = (local / "chromium.desktop").read_text()
    # No se oculta nada: va a «Opciones avanzadas» (categoría propia) y la acción no se toca
    assert "Categories=X-TISMA-Avanzado;" in c and "NoDisplay=false" in c
    assert m.avanzado("[Desktop Entry]\nCategories=System;Qt\n") == "[Desktop Entry]\nCategories=System;Qt;X-TISMA-Avanzado;\n"
    assert m.avanzado(m.avanzado("[Desktop Entry]\nCategories=A;\n")).count("X-TISMA-Avanzado") == 1
    p = (local / "com.github.jeromerobert.pdfarranger.desktop").read_text()
    assert "Name[es]=Organizar PDF" in p and "[Desktop Action new]\nName[es]=Ventana nueva" in p
    menu = (ROOT / "etc/xdg/menus/applications-merged/tisma.menu").read_text()
    for borrada in ("Science", "Graphics", "System", "Development", "Help", "Waydroid"):
        assert f"<Name>{borrada}</Name><Deleted/>" in menu
    assert menu.count("<Exclude><Category>X-TISMA-Avanzado</Category></Exclude>") == 6
    assert "Name=Opciones avanzadas" in (ROOT / "usr/share/desktop-directories/tisma-avanzado.directory").read_text()
    assert (ROOT / "usr/share/desktop-directories/tisma-herramientas.directory").read_text().count("Name=Herramientas")
    assert "lanzadores.py" in (ROOT.parent / "iso/build-iso.sh").read_text()


def test_bandeja_y_ajustes_una_vez():
    js = (ROOT / "usr/share/tisma/escritorio/bandeja.js").read_text()
    assert "hiddenItems" in js and "shownItems" not in js and "extraItems" not in js   # lo que la vaciaba
    s = (ROOT / "usr/lib/tisma/ajustes-escritorio").read_text()
    assert "bandeja-1" in s and (ROOT / "etc/xdg/autostart/tisma-ajustes-escritorio.desktop").exists()


def test_arranque_sin_estorbos():
    """Lo que no hace falta antes del escritorio va en la fase 2 o no arranca solo."""
    sb = (ROOT / "etc/systemd/system/tisma-secureboot.service").read_text()
    assert "WantedBy=tisma-fase2.target" in sb and "multi-user" not in sb
    b = (ROOT.parent / "iso/build-iso.sh").read_text()
    assert "systemctl disable lxc-net.service" in b and "systemctl reenable tisma-secureboot.service" in b
    for f in ("print-applet.desktop", "orca-autostart.desktop"):
        assert "Hidden=true" in (ROOT / "etc/xdg/autostart" / f).read_text()


def test_memoria_sin_congelarse():
    t = (ROOT / "usr/lib/tmpfiles.d/tisma-memoria.conf").read_text()
    assert "w- /sys/kernel/mm/lru_gen/min_ttl_ms - - - - 1000" in t and "w- /sys/kernel/mm/lru_gen/enabled" in t


def _precarga_personal():
    import importlib.machinery
    import importlib.util
    l = importlib.machinery.SourceFileLoader("pp", str(ROOT / "usr/lib/tisma/precarga-personal"))
    s = importlib.util.spec_from_loader("pp", l)
    m = importlib.util.module_from_spec(s)
    l.exec_module(m)
    return m


def test_precarga_aprende_de_cada_persona(tmp_path):
    import sqlite3
    m = _precarga_personal()
    db = tmp_path / "database"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE ResourceScoreCache (usedActivity TEXT, initiatingAgent TEXT, targettedResource TEXT, "
              "scoreType INTEGER, cachedScore FLOAT, firstUpdate INTEGER, lastUpdate INTEGER)")
    for res, sc in (("applications:net.tisma.Navegador.desktop", 9), ("applications:org.kde.dolphin.desktop", 5),
                    ("/home/ana/Documentos/tesis.odt", 50), ("applications:net.tisma.Navegador.desktop", 3)):
        c.execute("INSERT INTO ResourceScoreCache VALUES (':global', 'x', ?, 0, ?, 0, 0)", (res, sc))
    c.commit()
    c.close()
    assert m.mas_usadas(str(db)) == ["net.tisma.Navegador.desktop", "org.kde.dolphin.desktop"]   # sin documentos
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "net.tisma.Navegador.desktop").write_text("[Desktop Entry]\nExec=tisma-sandbox ejecutar navegador %U\n")
    (apps / "x.desktop").write_text("[Desktop Entry]\nExec=env FOO=1 /usr/bin/python3 -m x\n")
    assert m.programa_de("net.tisma.Navegador.desktop", [str(apps)]) == ["/usr/lib/chromium/chromium"]
    assert m.programa_de("x.desktop", [str(apps)])[0].startswith("/usr/")
    assert m.mas_usadas(str(tmp_path / "no-existe")) == []
    unidad = (ROOT / "usr/lib/systemd/user/tisma-precarga-personal.service").read_text()
    assert "IOSchedulingClass=idle" in unidad and "Nice=19" in unidad
    assert "konsole" not in (ROOT / "etc/tisma/precarga.conf").read_text()


def test_opensnitch_solo_al_abrirlo(tmp_path):
    """Sin reglas ni ventana no protegía nada y cada conexión pasaba por él: arranca al abrirlo desde el menú."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("lanzadores", ROOT.parent / "iso/lanzadores.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    apps = tmp_path / "usr/share/applications"
    apps.mkdir(parents=True)
    (apps / "opensnitch_ui.desktop").write_text("[Desktop Entry]\nExec=/usr/bin/opensnitch-ui\nTryExec=opensnitch-ui\n"
                                                "Categories=System;\n")
    m.aplicar(str(tmp_path))
    d = (tmp_path / "usr/local/share/applications/opensnitch_ui.desktop").read_text()
    assert "Exec=tisma-opensnitch\n" in d and "TryExec=tisma-opensnitch" in d and "X-TISMA-Avanzado" in d
    assert "systemctl disable opensnitch.service" in (ROOT.parent / "iso/build-iso.sh").read_text()
    w = (ROOT / "usr/bin/tisma-opensnitch").read_text()
    assert "pkexec /usr/bin/systemctl start opensnitch.service" in w and "exec opensnitch-ui" in w


def test_fwupd_no_queda_en_memoria():
    d = (ROOT / "etc/systemd/system/fwupd-refresh.service.d/60-tisma-cerrar-fwupd.conf").read_text()
    assert "ExecStopPost=-+/usr/bin/systemctl --no-block stop fwupd.service" in d
