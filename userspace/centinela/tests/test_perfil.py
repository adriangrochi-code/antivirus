"""Perfil de equipo firmado: vocabulario cerrado, límites duros, firma Ed25519, modelo y anti-retroceso."""
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from centinela import perfil

EJEMPLO = Path(__file__).resolve().parents[3] / "docs/perfiles/thinkpad-a275.hipotesis.json"
A275 = {"dmi_sys_vendor": "LENOVO", "dmi_product_name": "20KCS0FT02", "dmi_product_version": "ThinkPad A275",
        "cpu_modelo": "AMD A12-9800B R7"}


def test_ejemplo_valido_y_reglas():
    p = json.loads(EJEMPLO.read_text())
    assert perfil.validar(p) == []
    malo = copy.deepcopy(p)
    malo["limites_duros"]["mitigaciones"] = "off"
    malo["modos"]["maximo"]["inactivo"]["cpu.overclock"] = 10
    malo["modos"]["eco"]["inactivo"]["thp"] = "always"
    malo["agresivo"]["eco"]["inactivo"]["cpu.boost"] = 1                 # no está en agresivo_seguro
    e = " ".join(perfil.validar(malo))
    for frag in ("mitigaciones", "cpu.overclock", "thp", "no está en agresivo_seguro"):
        assert frag in e


def test_ajustes_agresivo_y_lista_negra():
    p = json.loads(EJEMPLO.read_text())
    eco = perfil.ajustes_de(p, "eco", "inactivo")
    assert eco["cpu.boost"] == 0 and "cpu.modulos_activos" not in eco
    assert perfil.ajustes_de(p, "eco", "inactivo", agresivo=True)["cpu.modulos_activos"] == 1
    p["lista_negra"] = ["pcie.aspm=powersupersave"]
    assert "pcie.aspm" not in perfil.ajustes_de(p, "eco", "inactivo")
    assert "gpu.dpm" not in perfil.ajustes_de(p, "eco", "inactivo", negra_local={"gpu.dpm"})


@pytest.mark.skipif(not shutil.which("openssl"), reason="sin openssl")
def test_firma_modelo_y_anti_retroceso(tmp_path):
    claves = tmp_path / "claves"
    claves.mkdir()
    priv = tmp_path / "priv.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(priv)], check=True)
    subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(claves / "tisma.pem")], check=True)
    p = json.loads(EJEMPLO.read_text())
    p["version"] = 2
    f = tmp_path / "a275.json"
    f.write_text(json.dumps(p))
    (tmp_path / "a275.json.sig").write_bytes(perfil.firmar(p, str(priv)))
    estado = tmp_path / "estado.json"
    assert perfil.cargar(str(f), estado, A275, claves)["version"] == 2
    with pytest.raises(perfil.PerfilInvalido, match="otro modelo"):
        perfil.cargar(str(f), estado, {**A275, "dmi_product_version": "ThinkPad E14"}, claves)
    p["modos"]["maximo"]["inactivo"]["cpu.boost"] = 0                   # cambiado después de firmar
    f.write_text(json.dumps(p))
    with pytest.raises(perfil.PerfilInvalido, match="firma"):
        perfil.cargar(str(f), estado, A275, claves)
    viejo = json.loads(EJEMPLO.read_text())                            # versión 1 < 2 ya aceptada
    f.write_text(json.dumps(viejo))
    (tmp_path / "a275.json.sig").write_bytes(perfil.firmar(viejo, str(priv)))
    with pytest.raises(perfil.PerfilInvalido, match="más vieja"):
        perfil.cargar(str(f), estado, A275, claves)
