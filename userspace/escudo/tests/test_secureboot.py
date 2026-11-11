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
