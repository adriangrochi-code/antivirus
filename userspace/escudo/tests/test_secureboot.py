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
    assert ["update-grub"] in ordenes


def test_debian_kernels_ordena_y_excluye(tmp_path):
    for n in ("vmlinuz-7.2.12+deb13-amd64", "vmlinuz-7.10.1+deb13-amd64", "vmlinuz-7.3.4-centinela-generic",
              "vmlinuz-7.2.12+deb13-amd64.old"):
        (tmp_path / n).write_text("x")
    ks = [os.path.basename(k) for k in mod.debian_kernels(str(tmp_path))]
    assert ks == ["vmlinuz-7.2.12+deb13-amd64", "vmlinuz-7.10.1+deb13-amd64"]
