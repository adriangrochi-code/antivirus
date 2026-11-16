"""VM de análisis: la rápida (microvm + kernel «analisis») si está instalada; si no, la clásica."""
from pathlib import Path

from escudo import detonar


def test_rapida_cuando_esta(tmp_path, monkeypatch):
    monkeypatch.setattr(detonar, "VM_DIR", tmp_path)
    assert not detonar.vm_rapida()
    for f in detonar.VM_RAPIDA:
        (tmp_path / f).write_bytes(b"x")
    assert detonar.vm_rapida()
    a = detonar.vm_argv(Path("/w"), 45, kvm=True)
    ap = a[a.index("-append") + 1]
    assert a[a.index("-M") + 1] == "microvm" and a[a.index("-m") + 1] == "512"
    assert "init=/usr/lib/tisma/init-analisis" in ap and "tisma.detonar=45" in ap and "tsc_early_khz" not in ap
    assert "-initrd" not in a and "-nic" in a and "none" in a
    assert "readonly=on" in a[a.index("-drive") + 1]
    assert "-enable-kvm" in a
    sin = detonar.vm_argv(Path("/w"), 45, kvm=False)
    assert "tsc_early_khz=2000000" in sin[sin.index("-append") + 1] and "-enable-kvm" not in sin


def test_clasica_si_no_hay_rapida(tmp_path, monkeypatch):
    monkeypatch.setattr(detonar, "VM_DIR", tmp_path)
    a = detonar.vm_argv(Path("/w"), 30)
    assert "-initrd" in a and a[a.index("-m") + 1] == "1536" and "boot=live" in a[a.index("-append") + 1]
