"""Drivers y firmware: qué se instala solo y qué no (DKMS requiere firma)."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ["TISMA_TEST"] = "1"
loader = importlib.machinery.SourceFileLoader("drivers", str(ROOT / "rootfs/usr/bin/tisma-drivers"))
spec = importlib.util.spec_from_loader("drivers", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)


class PC:
    def __init__(self, nvidia=False):
        self.nvidia, self.calls = nvidia, []

    def __call__(self, argv, timeout=600):
        self.calls.append(argv)
        if argv[0] == "isenkram-lookup":
            return 0, "firmware-iwlwifi\nbroadcom-sta-dkms\nfirmware-realtek\n"
        if argv[0] == "journalctl":
            return 0, ("iwlwifi 0000:03:00.0: Direct firmware load for iwlwifi-8265-36.ucode failed with error -2\n"
                       "bluetooth hci0: firmware: failed to load rtl_bt/rtl8822b_fw.bin (-2)\n")
        if argv[:2] == ["apt-file", "search"]:
            return 0, {"/lib/firmware/iwlwifi-8265-36.ucode": "firmware-iwlwifi\n",
                       "/lib/firmware/rtl_bt/rtl8822b_fw.bin": "firmware-realtek\n"}.get(argv[-1], "")
        if argv[0] == "dpkg-query":
            return 0, "firmware-realtek install ok installed\nfirmware-iwlwifi unknown ok not-installed\n"
        if argv[:2] == ["lspci", "-n"]:
            return 0, "01:00.0 0300: 10de:1c8d (rev a1)\n" if self.nvidia else "00:01.0 0300: 1002:9874\n"
        if argv[0] == "nvidia-detect":
            return 0, "It is recommended to install the\n    nvidia-driver\npackage.\n"
        return 0, ""


def test_plan_separates_firmware_from_dkms():
    pc = PC()
    p = mod.plan(pc)
    assert p["firmware_que_falto"] == ["iwlwifi-8265-36.ucode", "rtl_bt/rtl8822b_fw.bin"]
    assert p["instalar"] == ["firmware-iwlwifi"]                 # realtek ya estaba instalado
    assert p["manual_dkms"] == ["broadcom-sta-dkms"]             # nunca se instala solo
    assert p["nvidia"] is None
    assert ["apt-file", "update"] in pc.calls


def test_nvidia_advice_mentions_signing():
    msg = mod.plan(PC(nvidia=True))["nvidia"]
    assert "nouveau" in msg and "--firmar-dkms" in msg and "nvidia-driver" in msg


def test_shipped_auto_updates_cover_stable_point_releases():
    conf = (ROOT / "rootfs/etc/apt/apt.conf.d/52-tisma-actualizaciones").read_text()
    assert "codename=${distro_codename}-updates" in conf and "Automatic-Reboot \"false\"" in conf
