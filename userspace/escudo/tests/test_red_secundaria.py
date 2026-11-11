"""Reparto entre dos conexiones: el tráfico de segundo plano por la más lenta; nunca con VPN."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ["TISMA_TEST"] = "1"
loader = importlib.machinery.SourceFileLoader("red2", str(ROOT / "rootfs/usr/lib/tisma/red-secundaria"))
spec = importlib.util.spec_from_loader("red2", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)


class Net:
    def __init__(self, routes, vpn=False, wg=False):
        self.routes, self.vpn, self.wg, self.calls = routes, vpn, wg, []

    def __call__(self, argv, inp=None):
        self.calls.append((argv, inp))
        if argv[:4] == ["ip", "-j", "route", "show"]:
            return json.dumps(self.routes)
        if argv[:4] == ["nft", "list", "table", "inet"]:
            return "table inet tisma_vpn {}" if self.vpn else ""
        if argv[:4] == ["ip", "-j", "link", "show"]:
            return json.dumps([{"ifname": "wg0", "flags": ["UP"]}] if self.wg else [])
        if argv[:3] == ["iw", "dev", "wlan0"]:
            return "tx bitrate: 433.3 MBit/s" if argv[3] == "link" else "Interface wlan0"
        if argv[:3] == ["iw", "dev", "usb0"]:
            return ""
        return ""


ROUTES = [{"dst": "default", "gateway": "192.168.1.1", "dev": "wlan0", "metric": 600},
          {"dst": "default", "gateway": "192.168.42.129", "dev": "usb0", "metric": 100}]


def test_slow_link_gets_background_traffic(monkeypatch):
    monkeypatch.setattr(mod, "enabled", lambda path=None: True)
    n = Net(ROUTES)
    msg = mod.apply(n)
    assert "Segundo plano por usb0" in msg
    nft = next(i for a, i in n.calls if a[:2] == ["nft", "-f"])
    assert 'socket cgroupv2 level 3 "tisma.slice/tisma-segundo.slice/tisma-segundo-plano.slice"' in nft
    assert 'oifname "usb0" meta mark 0x2 masquerade' in nft
    assert (["ip", "route", "replace", "default", "via", "192.168.42.129", "dev", "usb0", "table", "200"], None) in n.calls
    assert (["sysctl", "-q", "net.ipv4.conf.usb0.rp_filter=2"], None) in n.calls


def test_never_with_vpn_or_single_link(monkeypatch):
    monkeypatch.setattr(mod, "enabled", lambda path=None: True)
    for n in (Net(ROUTES, vpn=True), Net(ROUTES, wg=True), Net(ROUTES[:1])):
        mod.apply(n)
        assert not any(a[:2] == ["nft", "-f"] for a, _ in n.calls)
        assert any(a[:4] == ["nft", "delete", "table", "inet"] for a, _ in n.calls)   # limpia lo anterior
