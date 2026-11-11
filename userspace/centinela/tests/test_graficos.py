"""Compatibilidad gráfica: detección por familia, capas de traducción y prueba de un arranque con GRUB."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from centinela import apply as ap  # noqa: E402
from centinela import graficos as g  # noqa: E402

VK_RADV = """Devices:
========
GPU0:
	apiVersion         = 1.4.311
	deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
	deviceName         = AMD Radeon HD 7850 (RADV PITCAIRN)
	driverName         = radv
GPU1:
	deviceType         = PHYSICAL_DEVICE_TYPE_CPU
	deviceName         = llvmpipe (LLVM 19.1.7, 256 bits)
	driverName         = llvmpipe
"""
VK_CPU = VK_RADV.split("GPU1:")[0].replace("DISCRETE_GPU", "CPU").replace("AMD Radeon HD 7850 (RADV PITCAIRN)", "llvmpipe")


def test_family_detection_uses_amdgpu_aliases(tmp_path):
    alias = tmp_path / "modules.alias"
    alias.write_text("alias pci:v00001002d00006818sv*sd*bc*sc*i* amdgpu\nalias pci:v00001002d00009874sv*sd*bc*sc*i* amdgpu\n"
                     "alias pci:v00001002d00006818sv*sd*bc*sc*i* radeon\n")
    ids = g.amdgpu_ids(path=str(alias))
    assert ids == {"6818", "9874"}
    gpus = [{"card": "card0", "vendor": "amd", "device_id": "0x6818", "driver": "radeon"},     # GCN 1 (Pitcairn)
            {"card": "card1", "vendor": "amd", "device_id": "0x68b8", "driver": "radeon"},     # TeraScale (Juniper)
            {"card": "card2", "vendor": "amd", "device_id": "0x9874", "driver": "amdgpu"},     # A275 (GCN 3)
            {"card": "card3", "vendor": "nvidia", "device_id": "0x1c82", "driver": "nouveau"}]
    c = g.classify(gpus, ids, [], "GP107")
    assert [x["cambiar_a_amdgpu"] for x in c] == [True, False, False, False]
    assert "TeraScale" in c[1]["familia"] and "amdgpu" in c[2]["familia"]
    assert "frecuencia" in c[3]["avisos"][0]


def test_translation_layers_by_vulkan_capability():
    radv = g.vulkan_devices(lambda a: VK_RADV)
    assert [d["driverName"] for d in radv] == ["radv", "llvmpipe"]
    d = g.decide([], radv)
    assert d["entorno"] == {"VK_LOADER_DRIVERS_DISABLE": "*lvp*"}          # GPU con Vulkan: DXVK y sin Lavapipe
    d = g.decide([], g.vulkan_devices(lambda a: VK_CPU))
    assert d["entorno"] == {"PROTON_USE_WINED3D": "1"}                     # solo CPU: WineD3D
    assert g.decide([], [])["entorno"] == {}                               # sin datos: no se toca nada


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(ap, "ETC", str(tmp_path / "etc"))
    return tmp_path


def fake_editenv(store):
    def run(argv, timeout=30):
        if argv[0] != "grub-editenv":
            return ""
        if argv[2] == "list":
            return "".join(f"{k}={v}\n" for k, v in store.items())
        if argv[2] == "set":
            k, v = argv[3].split("=", 1)
            store[k] = v
        return ""
    return run


def test_trial_boot_confirmed_or_discarded(state, monkeypatch):
    store = {}
    run = fake_editenv(store)
    info = {"cambiar_a_amdgpu": True, "entorno": {"PROTON_USE_WINED3D": "1"}, "razones": [], "avisos": []}
    msgs = g.apply(info, run, env_path=str(state / "esp/tisma/grubenv"))
    assert store["tisma_gpu"] == "probar" and "próximo arranque" in msgs[-1]
    assert (state / "etc/environment.d/60-tisma-graficos.conf").read_text().endswith("PROTON_USE_WINED3D=1\n")
    # Arranque normal antes de la prueba: nada cambia
    assert g.confirm(run, cmdline="quiet splash") == "en_prueba"
    # Arranque de prueba con imagen: GRUB lo había marcado «fallido»; se confirma
    store["tisma_gpu"] = "fallido"
    monkeypatch.setattr("centinela.hw.gpus", lambda: [{"vendor": "amd", "driver": "amdgpu"}])
    monkeypatch.setattr(g, "detect", lambda run=None: {"entorno": {"VK_LOADER_DRIVERS_DISABLE": "*lvp*"}})
    assert g.confirm(run, check_display=lambda: True, cmdline="quiet amdgpu.si_support=1") == "confirmado"
    assert store["tisma_gpu"] == "ok"
    assert "VK_LOADER" in (state / "etc/environment.d/60-tisma-graficos.conf").read_text()


def test_hung_trial_boot_is_reverted_without_retrying(state):
    store = {}
    run = fake_editenv(store)
    g.apply({"cambiar_a_amdgpu": True, "entorno": {}, "razones": [], "avisos": []}, run,
            env_path=str(state / "esp/tisma/grubenv"))
    store["tisma_gpu"] = "fallido"            # GRUB lo marcó y el arranque de prueba se colgó
    assert g.confirm(run, cmdline="quiet splash") == "revertido"   # el siguiente arranque va sin el cambio
    assert g.apply({"cambiar_a_amdgpu": True, "entorno": {}, "razones": [], "avisos": []}, run,
                   env_path=str(state / "esp/tisma/grubenv"))[-1].startswith("El cambio a amdgpu ya falló")


@pytest.mark.skipif(not shutil.which("grub-editenv"), reason="sin grub-editenv")
def test_real_grubenv_roundtrip(tmp_path):
    p = str(tmp_path / "tisma" / "grubenv")
    g.env_set(p, "probar")
    assert g.env_get(p) == "probar" and Path(p).stat().st_size == 1024     # tamaño fijo: GRUB puede reescribirlo
    g.env_set(p, "ok")
    assert g.env_get(p) == "ok"


@pytest.mark.skipif(not shutil.which("grub-script-check"), reason="sin grub-script-check")
def test_grub_snippet_is_valid(tmp_path):
    root = Path(__file__).resolve().parents[3]
    out = subprocess.run(["sh", str(root / "rootfs/etc/grub.d/05_tisma_graficos")], capture_output=True, text=True).stdout
    (tmp_path / "s.cfg").write_text(out)
    assert subprocess.run(["grub-script-check", str(tmp_path / "s.cfg")]).returncode == 0
    assert 'set tisma_gpu=fallido' in out.split("save_env")[0]        # se marca ANTES de arrancar a prueba
