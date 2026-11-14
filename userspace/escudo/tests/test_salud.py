"""Salud del hardware y del sistema: avisa solo cuando algo empeora."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ["TISMA_TEST"] = "1"
loader = importlib.machinery.SourceFileLoader("salud", str(ROOT / "rootfs/usr/lib/tisma/salud-hardware"))
spec = importlib.util.spec_from_loader("salud", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)


class Sys:
    def __init__(self):
        self.corr, self.smart_ok, self.ras_mce, self.failed, self.verify, self.klog = 0, True, 0, "", "", ""
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:3] == ["btrfs", "device", "stats"]:
            return 0, f"[/dev/mapper/luks-x].write_io_errs    0\n[/dev/mapper/luks-x].corruption_errs  {self.corr}\n"
        if argv == ["smartctl", "--scan"]:
            return 0, "/dev/nvme0 -d nvme # /dev/nvme0, NVMe device\n"
        if argv[:2] == ["smartctl", "-H"]:
            return 0, json.dumps({"smart_status": {"passed": self.smart_ok}})
        if argv[0] == "ras-mc-ctl":
            mce = "No MCE errors." if not self.ras_mce else f"MCE records summary:\n\t{self.ras_mce} Corrected errors\n"
            return 0, f"No Memory errors.\n{mce}\nNo PCIe AER errors.\n"
        if argv[:2] == ["systemctl", "--failed"]:
            return 0, self.failed
        if argv == ["dpkg", "--verify"]:
            return 0, self.verify
        if argv[0] == "journalctl":
            return 0, self.klog
        if argv[:2] == ["dpkg", "-S"]:
            return 0, "libc6:amd64: " + argv[2] + "\n"
        return 0, ""


def test_alerts_only_on_change(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(mod, "ALERTS", str(tmp_path / "a.jsonl"))
    monkeypatch.setattr(mod, "alert", lambda *a, **k: alerts.append(a))
    alerts = []
    s = Sys()
    assert mod.semanal(s) == [] and alerts == []
    s.corr, s.ras_mce, s.smart_ok = 3, 2, False
    s.failed = "escudo.service loaded failed failed Escudo\n"
    s.verify = "??5??????   /usr/lib/x86_64-linux-gnu/libc.so.6\n??5?????? c /etc/ssh/sshd_config\n"
    s.klog = "amdgpu 0000:00:01.0: amdgpu: ring gfx timeout, signaled seq=1\n"
    msgs = mod.semanal(s)
    kinds = {a[0] for a in alerts}
    assert {"disco", "hardware", "sistema"} <= kinds and len(msgs) == 6
    assert any("libc.so.6" in a[2] for a in alerts) and not any("sshd_config" in a[2] for a in alerts)
    alerts.clear()
    s.klog = ""
    mod.semanal(s)                       # nada nuevo: no se repiten los avisos
    assert alerts == []
    assert mod.altered_files(s) == ["/usr/lib/x86_64-linux-gnu/libc.so.6"]


def test_repair_reinstalls_owner_packages():
    s = Sys()
    s.verify = "??5??????   /usr/lib/x86_64-linux-gnu/libc.so.6\n"
    out = mod.reparar(s)
    assert ["apt-get", "install", "--reinstall", "-y", "-q", "libc6"] in s.calls and "libc6" in out


def test_alert_file_not_followed_through_symlink(tmp_path):
    target = tmp_path / "victima"
    target.write_text("x")
    link = tmp_path / "a.jsonl"
    link.symlink_to(target)
    try:
        mod.alert("disco", "low", "d", "a", path=str(link))
        raise AssertionError("debería negarse a seguir el enlace")
    except OSError:
        pass
    assert target.read_text() == "x"


def test_ssd_wear_nvme_and_sata_alert_once_per_level(tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setattr(mod, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(mod, "ALERTS", str(tmp_path / "a.jsonl"))
    alerts = []
    monkeypatch.setattr(mod, "alert", lambda *a, **k: alerts.append(a))
    used = {"nvme": 83}

    def run(argv):
        if argv[:2] == ["smartctl", "--scan"]:
            return 0, "/dev/nvme0 -d nvme # nvme\n/dev/sda -d sat # sata\n"
        if argv[:2] == ["smartctl", "-a"]:
            if argv[-1] == "/dev/nvme0":
                return 0, _json.dumps({"nvme_smart_health_information_log": {"percentage_used": used["nvme"]}})
            return 0, _json.dumps({"ata_smart_attributes": {"table": [{"id": 9, "value": 99}, {"id": 177, "value": 40}]}})
        if argv[:2] == ["smartctl", "-H"]:
            return 0, _json.dumps({"smart_status": {"passed": True}})
        return 0, ""
    assert mod.smart_wear(run) == {"/dev/nvme0": 83, "/dev/sda": 60}
    mod.semanal(run)
    assert [a[1] for a in alerts if "vida útil" in a[2]] == ["medium"]          # solo el NVMe pasa del 80 %
    mod.semanal(run)
    assert len([a for a in alerts if "vida útil" in a[2]]) == 1                 # no se repite
    used["nvme"] = 96
    mod.semanal(run)
    assert [a[1] for a in alerts if "vida útil" in a[2]] == ["medium", "high"]


def test_calor_avisa_una_vez_al_mes(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(mod, "alert", lambda *a, **k: alerts.append(a))
    f = tmp_path / "calor.json"
    f.write_text(json.dumps({"2026-11-10": {"ocupado_s": 3600, "frenado_s": 1500, "max_c": 95}}))
    st = {}
    assert "25 min" in mod.calor_frenado(st, str(f), ahora=lambda: 2e9)
    assert alerts[0][0] == "calor" and "ventilador" in alerts[0][3]
    assert mod.calor_frenado(st, str(f), ahora=lambda: 2e9 + 86400) == ""          # no se repite enseguida
    f.write_text(json.dumps({"2026-11-10": {"ocupado_s": 36000, "frenado_s": 900}}))
    assert mod.calor_frenado({}, str(f)) == ""                                      # 2,5 %: picos aislados
