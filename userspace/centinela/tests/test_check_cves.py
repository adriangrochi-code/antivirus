"""kernel/check-cves.py: los rangos de kernel.org se solapan (uno por rama); no inflar el recuento."""
import importlib.util
import io
import json
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("check_cves", RAIZ / "kernel/check-cves.py")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)


def _zip(entradas: dict) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for cve, rangos in entradas.items():
            ranges = [{"type": "ECOSYSTEM", "events": [{"introduced": a}] + ([{"fixed": b}] if b else [])}
                      for a, b in rangos]
            z.writestr(f"{cve}.json", json.dumps({"id": cve, "summary": "net: algo", "affected": [{"ranges": ranges}]}))
    buf.seek(0)
    return zipfile.ZipFile(buf)


def ids(z, v):
    return {c["id"]: c["fixed_in"] for c in cc.affecting(z, v)}


def test_fix_in_our_branch_wins_over_overlapping_ranges():
    # Caso real (CVE-2026-23220): corregido en 6.18.11, pero otro rango dice «6.13 → 6.19.1»
    z = _zip({"CVE-A": [("6.7.0", "6.18.11"), ("6.13.0", "6.19.1")]})
    assert ids(z, "6.18.63") == {}                       # antes contaba como abierto
    assert ids(z, "6.18.5") == {"CVE-A": "6.18.11"}      # versión anterior al arreglo: afectado, con arreglo
    assert ids(z, "6.19.3") == {}


def test_fixed_only_in_newer_branch_is_open_without_branch_fix():
    z = _zip({"CVE-B": [("6.17.0", "7.2.6")]})           # nunca llevado a la 6.18
    assert ids(z, "6.18.63") == {"CVE-B": None}
    assert ids(z, "7.2.12") == {}
    assert ids(z, "6.12.100") == {}                      # introducido después


def test_unfixed_everywhere_and_older_branch_fixes():
    z = _zip({"CVE-C": [("5.10.0", None)], "CVE-D": [("5.10.0", "6.1.50")]})
    assert ids(z, "7.3.4") == {"CVE-C": None}            # sin arreglo en ninguna parte
    assert ids(z, "6.1.40") == {"CVE-C": None, "CVE-D": "6.1.50"}
