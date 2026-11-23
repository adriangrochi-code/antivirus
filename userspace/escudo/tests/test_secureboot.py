"""tisma-secureboot: qué kernel arranca según Secure Boot, la clave TISMA y la firma."""
import pytest
import importlib.machinery
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ["TISMA_TEST"] = "1"
loader = importlib.machinery.SourceFileLoader("sb", str(ROOT / "rootfs/usr/bin/tisma-secureboot"))
spec = importlib.util.spec_from_loader("sb", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

K = ["/boot/vmlinuz-6.18.63-centinela-generic"]
KM = ["/boot/vmlinuz-6.18.63-centinela-minimal"]


def test_choice_never_risks_an_unbootable_system():
    assert mod.choose(False, False, K, {}, vm=False) == K[0]                     # sin Secure Boot: TISMA
    assert mod.choose(True, False, K, {K[0]: True}, vm=False) is None            # clave no registrada: Debian
    assert mod.choose(True, True, K, {K[0]: False}, vm=False) is None            # kernel sin firmar: Debian
    assert mod.choose(True, True, K, {K[0]: True}, vm=False) == K[0]             # todo en orden: TISMA
    assert mod.choose(None, False, K, {}, vm=False) is None                      # no se sabe: lo seguro
    assert mod.choose(False, False, KM, {}, vm=False) is None                    # minimal en hardware real: no
    assert mod.choose(False, False, KM, {}, vm=True) == KM[0]                    # minimal en máquina virtual: sí
    assert mod.choose(False, False, [], {}, vm=False) is None


def test_sb_state_parsing():
    assert mod.sb_enabled(lambda a: (0, "SecureBoot enabled\n")) is True
    assert mod.sb_enabled(lambda a: (0, "SecureBoot disabled\n")) is False
    assert mod.sb_enabled(lambda a: (1, "EFI variables are not supported on this system\n")) is False
    assert mod.sb_enabled(lambda a: (1, "")) is None


def test_elegir_marca_siempre_un_kernel(tmp_path, monkeypatch):
    """Sin marcar nada, GRUB arranca el kernel de versión más alta (el TISMA 7.3 > Debian 7.2): con Secure Boot y la
    clave sin registrar, el primer arranque fallaba («bad shim signature»)."""
    boot = tmp_path / "boot"
    boot.mkdir()
    for n in ("vmlinuz-7.2.12+deb13-amd64", "vmlinuz-7.1.4+deb13-amd64", "vmlinuz-7.3.4-centinela-minimal",
              "vmlinuz-7.3.4-centinela-generic"):
        (boot / n).write_text("x")
    cfg = tmp_path / "62-tisma-kernel.cfg"
    monkeypatch.setattr(mod, "BOOT", str(boot))
    monkeypatch.setattr(mod, "GRUB_CFG", str(cfg))
    monkeypatch.setattr(mod, "GRUB_MENU", str(tmp_path / "grub.cfg"))
    monkeypatch.setattr(mod, "tisma_kernels", lambda: sorted(str(p) for p in boot.glob("*centinela*")))
    monkeypatch.setattr(mod, "debian_kernels", lambda: [str(boot / "vmlinuz-7.1.4+deb13-amd64"),
                                                       str(boot / "vmlinuz-7.2.12+deb13-amd64")])
    ordenes = []

    def run(argv, inp=None):
        ordenes.append(argv)
        if argv[:2] == ["mokutil", "--sb-state"]:
            return 0, "SecureBoot enabled\n"
        if argv[:2] == ["mokutil", "--test-key"]:
            return 0, "is not enrolled"
        if argv[0] == "systemd-detect-virt":
            return 1, "none"
        return 0, ""
    r = mod.elegir(run)
    assert "Debian" in r and "7.2.12" in r
    assert 'GRUB_TOP_LEVEL="' + str(boot / "vmlinuz-7.2.12+deb13-amd64") + '"' in cfg.read_text()
    assert ["/usr/sbin/update-grub"] in ordenes            # ruta completa: el instalador puede no tener /usr/sbin


def _entorno(tmp_path, monkeypatch, menu_primero: str, cfg_previa: str | None):
    boot = tmp_path / "boot"
    boot.mkdir()
    deb = boot / "vmlinuz-7.2.12+deb13-amd64"
    deb.write_text("x")
    (boot / "vmlinuz-7.3.4-centinela-minimal").write_text("x")
    cfg = tmp_path / "62-tisma-kernel.cfg"
    if cfg_previa is not None:
        cfg.write_text(cfg_previa.format(deb=deb))
    menu = tmp_path / "grub.cfg"
    version = menu_primero.replace("vmlinuz-", "")
    (boot / f"initrd.img-{version}").write_text("i")
    menu.write_text(f"menuentry 'TISMA' {{\n\tlinux\t/{menu_primero} root=UUID=x ro quiet\n"
                    f"\tinitrd\t/initrd.img-{version}\n}}\n")
    monkeypatch.setattr(mod, "BOOT", str(boot))
    monkeypatch.setattr(mod, "MODULES", str(tmp_path / "sin-modulos"))
    monkeypatch.setattr(mod, "GRUB_CFG", str(cfg))
    monkeypatch.setattr(mod, "GRUB_MENU", str(menu))
    monkeypatch.setattr(mod, "tisma_kernels", lambda: [str(boot / "vmlinuz-7.3.4-centinela-minimal")])
    monkeypatch.setattr(mod, "debian_kernels", lambda: [str(deb)])
    ordenes = []

    def run(argv, inp=None):
        ordenes.append(argv)
        if argv[:2] == ["mokutil", "--sb-state"]:
            return 0, "SecureBoot enabled\n"
        if argv[0] == "systemd-detect-virt":
            return 1, "none"
        return 0, ""
    return run, ordenes


def test_elegir_rehace_el_menu_si_arranca_otro_kernel(tmp_path, monkeypatch):
    """A275 (preview 6): la configuración ya decía Debian pero el menú de GRUB seguía empezando por el kernel TISMA
    minimal («firma shim incorrecta»). Como la configuración «no cambiaba», el menú nunca se regeneraba."""
    previa = '# Generado por tisma-secureboot elegir\nGRUB_TOP_LEVEL="{deb}"\n'
    run, ordenes = _entorno(tmp_path, monkeypatch, "vmlinuz-7.3.4-centinela-minimal", previa)
    mod.elegir(run)
    assert ["/usr/sbin/update-grub"] in ordenes


def test_elegir_no_toca_un_menu_correcto(tmp_path, monkeypatch):
    previa = '# Generado por tisma-secureboot elegir\nGRUB_TOP_LEVEL="{deb}"\n'
    run, ordenes = _entorno(tmp_path, monkeypatch, "vmlinuz-7.2.12+deb13-amd64", previa)
    mod.elegir(run)
    assert ["/usr/sbin/update-grub"] not in ordenes


def test_elegir_avisa_si_update_grub_falla(tmp_path, monkeypatch):
    run, _ = _entorno(tmp_path, monkeypatch, "vmlinuz-7.3.4-centinela-minimal", None)

    def run_falla(argv, inp=None):
        if argv[0] == "/usr/sbin/update-grub":
            return 1, "grub-probe: error: failed to get canonical path"
        return run(argv, inp)
    try:
        mod.elegir(run_falla)
    except RuntimeError as e:
        assert "update-grub falló" in str(e)
    else:
        raise AssertionError("un update-grub fallido no puede pasar en silencio")


def test_quitar_minimal_solo_en_equipos_reales():
    instalados = "linux-image-7.3.4-centinela-minimal ii \nlinux-headers-7.3.4-centinela-minimal ii \n"

    def hacer(vm):
        ordenes = []

        def run(argv, inp=None):
            ordenes.append(argv)
            if argv[0] == "systemd-detect-virt":
                return (0, "kvm") if vm else (1, "none")
            if argv[0] == "dpkg-query":
                return 0, instalados
            return 0, ""
        return mod.quitar_minimal(run), ordenes
    r, ordenes = hacer(vm=False)
    assert ["apt-get", "purge", "-y", "-q", "linux-image-7.3.4-centinela-minimal",
            "linux-headers-7.3.4-centinela-minimal"] in ordenes and "quitado" in r
    r, ordenes = hacer(vm=True)
    assert not any(o[0] == "apt-get" for o in ordenes)


def test_kernel_nuevo_vuelve_a_elegir():
    for d in ("postinst.d", "postrm.d"):
        h = ROOT / "rootfs/etc/kernel" / d / "zzz-tisma-secureboot"
        assert os.access(h, os.X_OK) and "tisma-secureboot elegir" in h.read_text()
        assert sorted(["zz-update-grub", h.name])[-1] == h.name          # después de zz-update-grub
    sp = (ROOT / "rootfs/etc/calamares/modules/shellprocess-tisma.conf").read_text()
    assert sp.index("quitar-minimal") < sp.index("secureboot elegir") < sp.index("perfil completar")


def test_debian_kernels_ordena_y_excluye(tmp_path):
    for n in ("vmlinuz-7.2.12+deb13-amd64", "vmlinuz-7.10.1+deb13-amd64", "vmlinuz-7.3.4-centinela-generic",
              "vmlinuz-7.2.12+deb13-amd64.old"):
        (tmp_path / n).write_text("x")
    ks = [os.path.basename(k) for k in mod.debian_kernels(str(tmp_path))]
    assert ks == ["vmlinuz-7.2.12+deb13-amd64", "vmlinuz-7.10.1+deb13-amd64"]


def test_elegir_rehace_el_menu_si_la_entrada_no_carga_su_initramfs(tmp_path, monkeypatch):
    """El instalador arma el menú ANTES de crear los initramfs. Con el kernel correcto ya primero (preview 16),
    elegir no lo regeneraba y la entrada quedaba sin initramfs: el kernel no podía abrir el disco."""
    previa = '# Generado por tisma-secureboot elegir\nGRUB_TOP_LEVEL="{deb}"\n'
    run, ordenes = _entorno(tmp_path, monkeypatch, "vmlinuz-7.2.12+deb13-amd64", previa)
    (tmp_path / "boot" / "initrd.img-7.2.12+deb13-amd64").write_text("x")      # el initramfs ya existe
    (tmp_path / "grub.cfg").write_text("menuentry 'TISMA' {\n\tlinux\t/vmlinuz-7.2.12+deb13-amd64 ro\n}\n")  # sin initrd
    mod.elegir(run)
    assert ["/usr/sbin/update-grub"] in ordenes
    # con la línea initrd en el menú, ya no se toca
    menu = tmp_path / "grub.cfg"
    menu.write_text("menuentry 'TISMA' {\n\tlinux\t/vmlinuz-7.2.12+deb13-amd64 ro\n\tinitrd\t/initrd.img-7.2.12+deb13-amd64\n}\n")
    ordenes.clear()
    mod.elegir(run)
    assert ["/usr/sbin/update-grub"] not in ordenes



def test_crea_el_initramfs_que_falta_y_rehace_el_menu(tmp_path, monkeypatch):
    """A275 (preview 19): «VFS: Unable to mount root fs … unknown-block(0,0)»: el menú no cargaba initramfs y, si /boot
    no tenía NINGUNO, se daba por bueno. Ahora se crea el que falte y una entrada sin initrd rehace el menú."""
    boot, mods = tmp_path / "boot", tmp_path / "modules"
    boot.mkdir()
    (mods / "7.2.12+deb13-amd64").mkdir(parents=True)
    (boot / "vmlinuz-7.2.12+deb13-amd64").write_text("x")
    hechos = []

    def run(argv, inp=None):
        hechos.append(argv)
        if argv[0] == mod.UPDATE_INITRAMFS:
            (boot / f"initrd.img-{argv[-1]}").write_text("i")
        return 0, ""
    assert mod.asegurar_initrd(run, str(boot), str(mods)) == ["7.2.12+deb13-amd64"]
    assert mod.asegurar_initrd(run, str(boot), str(mods)) == []                 # ya está: no se rehace
    menu = tmp_path / "grub.cfg"
    menu.write_text("menuentry 'Debian' {\n\tlinux\t/vmlinuz-7.2.12+deb13-amd64 root=/dev/sda3 ro quiet\n}\n")
    assert mod.primer_con_initrd(str(menu), str(tmp_path / "boot-vacio")) is False   # sin initrd: no arrancaría
    with pytest.raises(RuntimeError):
        mod.asegurar_initrd(lambda a, i=None: (1, "sin espacio"), str(tmp_path / "b2"), str(mods)) if \
            (tmp_path / "b2").mkdir() or (tmp_path / "b2" / "vmlinuz-7.2.12+deb13-amd64").write_text("x") else None


# =====================================================================================================================
# ===                                 Installer final check («verificar»)                                         ===
# =====================================================================================================================

ZSTD_INITRD = b"\x28\xb5\x2f\xfd" + b"\0" * (mod.INITRD_MIN_BYTES + 16)    # smallest image the check accepts


def _sistema(tmp_path, initrd: bytes | None = ZSTD_INITRD, con_linea_initrd=True):
    """Fake installed system: /boot with a Debian kernel, a grub.cfg and an ESP with one GRUB stub."""
    boot, esp = tmp_path / "boot", tmp_path / "esp"
    (boot / "grub").mkdir(parents=True)
    (esp / "EFI" / "TISMA").mkdir(parents=True)
    version = "7.3.4+deb13-amd64"
    (boot / f"vmlinuz-{version}").write_text("k")
    if initrd is not None:
        (boot / f"initrd.img-{version}").write_bytes(initrd)
    linea = f"\tinitrd\t/initrd.img-{version}\n" if con_linea_initrd else ""
    (boot / "grub" / "grub.cfg").write_text(
        f"menuentry 'TISMA' {{\n\tlinux\t/vmlinuz-{version} root=UUID=r ro quiet\n{linea}}}\n")
    (esp / "EFI" / "TISMA" / "grub.cfg").write_text(
        "search.fs_uuid 9bfe-boot root hd0,gpt2\nset prefix=($root)'/grub'\nconfigfile $prefix/grub.cfg\n")
    return boot, esp


def _problemas(boot, esp, existentes=("9bfe-boot",)):
    return mod.problemas_de_arranque(str(boot / "grub" / "grub.cfg"), str(boot), str(esp),
                                     lambda: "9bfe-boot", lambda u: u in existentes)


def test_verificar_acepta_un_sistema_que_arranca(tmp_path):
    boot, esp = _sistema(tmp_path)
    assert _problemas(boot, esp) == []


def test_verificar_no_da_por_bueno_un_menu_que_no_existe(tmp_path):
    """A275 (preview 22): sin grub.cfg la comprobación decía «Arranque comprobado: ?» y el sistema no arrancaba."""
    boot, esp = _sistema(tmp_path)
    (boot / "grub" / "grub.cfg").unlink()
    assert any("no existe el menú" in p for p in _problemas(boot, esp))
    assert mod.primer_con_initrd(str(boot / "grub" / "grub.cfg"), str(boot)) is False
    (boot / "grub" / "grub.cfg").write_text("set timeout=1\n")            # a menu with no Linux entry
    assert any("ninguna entrada" in p for p in _problemas(boot, esp))


def test_verificar_detecta_initramfs_ausente_vacio_o_danado(tmp_path):
    boot, esp = _sistema(tmp_path, con_linea_initrd=False)
    assert any("no carga ningún initramfs" in p for p in _problemas(boot, esp))
    boot, esp = _sistema(tmp_path / "b", initrd=None)
    assert any("falta el initramfs" in p for p in _problemas(boot, esp))
    boot, esp = _sistema(tmp_path / "c", initrd=b"\x28\xb5\x2f\xfd" + b"\0" * 100)    # truncated
    assert any("incompleto" in p for p in _problemas(boot, esp))
    boot, esp = _sistema(tmp_path / "d", initrd=b"\0" * (mod.INITRD_MIN_BYTES + 10))   # zeros: not an image
    assert any("dañado" in p for p in _problemas(boot, esp))


def test_stub_efi_de_una_instalacion_anterior(tmp_path):
    """Un stub de GRUB en la partición EFI que apunta a un /boot borrado se detecta y se corrige; el de otro sistema
    que sigue existiendo (arranque dual) no se toca."""
    boot, esp = _sistema(tmp_path)
    viejo = esp / "EFI" / "debian" / "grub.cfg"
    viejo.parent.mkdir()
    viejo.write_text("search.fs_uuid dead-0ld root\nconfigfile ($root)/grub/grub.cfg\n")
    otro = esp / "EFI" / "otro" / "grub.cfg"
    otro.parent.mkdir()
    otro.write_text("search.fs_uuid otro-sistema root\n")
    problemas = _problemas(boot, esp, existentes=("9bfe-boot", "otro-sistema"))
    assert len(problemas) == 1 and "EFI/debian/grub.cfg" in problemas[0]
    rotos, _ = mod.stubs_efi_rotos(str(esp), lambda: "9bfe-boot", lambda u: u in ("9bfe-boot", "otro-sistema"))
    mod.reparar_stubs_efi(rotos, "9bfe-boot")
    assert "search.fs_uuid 9bfe-boot root" in viejo.read_text()
    assert "otro-sistema" in otro.read_text()


def test_reponer_kernel_en_modules(tmp_path):
    """Con /boot en otra partición, los enlaces duros del kernel 7.x no se pueden copiar: se reponen como archivos."""
    boot, mods = tmp_path / "boot", tmp_path / "modules"
    boot.mkdir()
    (mods / "7.3.4+deb13-amd64").mkdir(parents=True)
    (boot / "vmlinuz-7.3.4+deb13-amd64").write_text("k")
    (boot / "config-7.3.4+deb13-amd64").write_text("c")
    assert mod.reponer_kernel_en_modules(str(boot), str(mods)) == ["7.3.4+deb13-amd64/vmlinuz",
                                                                   "7.3.4+deb13-amd64/config"]
    assert (mods / "7.3.4+deb13-amd64" / "vmlinuz").read_text() == "k"
    assert mod.reponer_kernel_en_modules(str(boot), str(mods)) == []


def test_verificar_repara_una_vez_y_si_no_falla(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "MODULES", str(tmp_path / "sin-modulos"))
    monkeypatch.setattr(mod, "BOOT", str(tmp_path / "boot"))
    monkeypatch.setattr(mod, "ESP", str(tmp_path / "esp"))
    monkeypatch.setattr(mod, "uuid_de_boot_real", lambda: None)
    ordenes = []

    def run(argv, inp=None):
        ordenes.append(argv)
        return 0, ""
    estado = {"problemas": ["la entrada vmlinuz-x no carga ningún initramfs"]}

    def comprobar():
        return list(estado["problemas"])

    def run_que_arregla(argv, inp=None):
        if argv == [mod.UPDATE_GRUB]:
            estado["problemas"] = []
        return run(argv, inp)
    mod.verificar(run_que_arregla, comprobar)
    assert [mod.UPDATE_GRUB] in ordenes and ["sync"] in ordenes

    estado["problemas"] = ["falta el initramfs initrd.img-x"]
    with pytest.raises(RuntimeError, match="no podría arrancar"):
        mod.verificar(run, comprobar)                     # the repair did not help: the installer must show it
