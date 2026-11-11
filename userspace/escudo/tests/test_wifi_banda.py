"""Cambio de banda 2,4/5 GHz con histéresis."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ["TISMA_TEST"] = "1"
loader = importlib.machinery.SourceFileLoader("banda", str(ROOT / "rootfs/usr/lib/tisma/wifi-banda"))
spec = importlib.util.spec_from_loader("banda", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

LINK_24 = "Connected to aa:aa:aa:aa:aa:01 (on wlan0)\n\tSSID: Casa\n\tfreq: 2437\n\tsignal: -50 dBm\n"
LINK_5 = "Connected to aa:aa:aa:aa:aa:02 (on wlan0)\n\tSSID: Casa\n\tfreq: 5180\n\tsignal: -81 dBm\n"
SCAN = ("BSS aa:aa:aa:aa:aa:01(on wlan0)\n\tfreq: 2437\n\tsignal: -50.00 dBm\n\tlast seen: 100 ms ago\n\tSSID: Casa\n"
        "BSS aa:aa:aa:aa:aa:02(on wlan0)\n\tfreq: 5180\n\tsignal: {s5} dBm\n\tlast seen: 200 ms ago\n\tSSID: Casa\n"
        "BSS bb:bb:bb:bb:bb:03(on wlan0)\n\tfreq: 5500\n\tsignal: -40.00 dBm\n\tlast seen: 100 ms ago\n\tSSID: Vecino\n")


class Radio:
    def __init__(self, link, s5="-60.00", roam_ok=True):
        self.link, self.s5, self.roam_ok, self.calls = link, s5, roam_ok, []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[-1] == "link":
            return self.link
        if argv[-1] == "dump":
            return SCAN.format(s5=self.s5)
        if argv[0] == "wpa_cli":
            return "OK\n" if self.roam_ok else "FAIL\n"
        if argv[:6] == ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"]:
            return "Casa:802-11-wireless\nCable:802-3-ethernet\n"
        if argv[:2] == ["nmcli", "-g"]:
            return "Casa\n"
        return ""


def test_moves_to_5ghz_after_confirmation_and_respects_gap():
    r, t = Radio(LINK_24), [1000.0]
    s = mod.Steer("wlan0", run=r, now=lambda: t[0])
    assert s.step() is None                                   # primera vez: solo se anota
    t[0] += 10
    msg = s.step()
    assert msg and "→ Casa 5+ GHz" in msg and ["wpa_cli", "-i", "wlan0", "roam", "aa:aa:aa:aa:aa:02"] in r.calls
    t[0] += 10
    s.step()
    t[0] += 10
    assert s.step() is None                                   # menos de 90 s desde el último cambio


def test_weak_5ghz_signal_stays_on_24():
    r = Radio(LINK_24, s5="-75.00")
    s = mod.Steer("wlan0", run=r, now=lambda: 0.0)
    assert s.step() is None and s.step() is None
    assert not any(c[0] == "wpa_cli" for c in r.calls)


def test_falls_back_to_24_when_5ghz_fades_and_nmcli_if_no_wpa_cli():
    r, t = Radio(LINK_5, roam_ok=False), [0.0]
    s = mod.Steer("wlan0", run=r, now=lambda: t[0])
    s.step()
    t[0] += 10
    msg = s.step()
    assert msg and "2.4 GHz" in msg.split("→")[1]
    assert ["nmcli", "--wait", "15", "connection", "up", "id", "Casa", "ifname", "wlan0", "ap",
            "aa:aa:aa:aa:aa:01"] in r.calls


def test_pairs_by_name_pattern_and_config():
    assert mod.related("Casa", "Casa-5G", {}) and mod.related("Casa_5GHz", "Casa", {})
    assert not mod.related("Casa", "Vecino", {})
    assert mod.related("MiRed", "Rapida", {"MiRed": "Rapida"})


def test_polls_slowly_when_switching_cannot_help():
    good5 = "Connected to aa:aa:aa:aa:aa:02 (on wlan0)\n\tSSID: Casa\n\tfreq: 5180\n\tsignal: -55 dBm\n"
    st = mod.Steer("wlan0", run=Radio(good5))
    st.step({})
    assert st.next_poll() == 30                              # 5 GHz con buena señal
    st = mod.Steer("wlan0", run=Radio(LINK_24))
    st.step({})
    assert st.next_poll() == mod.POLL_S                      # en 2,4 GHz: vigilar si llega la de 5
    st = mod.Steer("wlan0", run=Radio(""))
    st.step({})
    assert st.next_poll() == 30                              # sin conexión
