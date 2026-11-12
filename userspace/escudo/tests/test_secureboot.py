"""tisma-secureboot: qué kernel arranca según Secure Boot, la clave TISMA y la firma."""
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
    menu.write_text(f"menuentry 'TISMA' {{\n\tlinux\t/{menu_primero} root=UUID=x ro quiet\n}}\n")
    monkeypatch.setattr(mod, "BOOT", str(boot))
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
