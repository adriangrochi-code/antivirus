"""Instalador al estilo Windows: región automática (instalar-region)."""
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path

RAIZ_REPO = Path(__file__).resolve().parents[3]
os.environ["TISMA_TEST"] = "1"
_l = importlib.machinery.SourceFileLoader("instalar_region", str(RAIZ_REPO / "rootfs/usr/lib/tisma/instalar-region"))
_s = importlib.util.spec_from_loader("instalar_region", _l)
R = importlib.util.module_from_spec(_s)
_l.exec_module(R)


def _destino(tmp: Path) -> Path:
    d = tmp / "destino"
    zi = d / "usr/share/zoneinfo"
    for z in ("Europe/Madrid", "America/Montevideo", "America/Argentina/Buenos_Aires", "Etc/UTC", "Asia/Tokyo"):
        (zi / z).parent.mkdir(parents=True, exist_ok=True)
        (zi / z).write_bytes(b"TZif")
    (zi / "zone.tab").write_text("# cc\tcoord\tTZ\nES\t+4024-00341\tEurope/Madrid\nUY\t-345858-0561245\t"
                                 "America/Montevideo\nAR\t-3436-05827\tAmerica/Argentina/Buenos_Aires\n"
                                 "JP\t+353916+1394441\tAsia/Tokyo\n")
    (d / "usr/share/i18n").mkdir(parents=True)
    (d / "usr/share/i18n/SUPPORTED").write_text("es_ES.UTF-8 UTF-8\nes_UY.UTF-8 UTF-8\nes_AR.UTF-8 UTF-8\n")
    (d / "etc").mkdir()
    (d / "etc/locale.gen").write_text("# es_AR.UTF-8 UTF-8\n# es_ES.UTF-8 UTF-8\n# en_US.UTF-8 UTF-8\n")
    (zi.parent.parent.parent / "etc").mkdir(exist_ok=True)
    return d


def _sesion(tmp: Path, zona="Europe/Madrid") -> Path:
    e = tmp / "live-etc"
    (e / "default").mkdir(parents=True)
    (e / "timezone").write_text(zona + "\n")
    (e / "default/keyboard").write_text('XKBMODEL="pc105"\nXKBLAYOUT="latam"\nXKBVARIANT=""\n')
    return e


def _red(respuesta):
    def abrir(url, timeout):
        assert url.startswith("https://") and timeout <= 5
        if isinstance(respuesta, Exception):
            raise respuesta
        return io.BytesIO(json.dumps(respuesta).encode())
    return abrir


def test_region_por_ubicacion(tmp_path):
    d, e = _destino(tmp_path), _sesion(tmp_path)
    ordenes = []
    assert R.main([str(d)], abrir=_red({"time_zone": "America/Montevideo"}), run=lambda a, **k: ordenes.append(a),
                  etc=str(e)) == 0
    assert (d / "etc/timezone").read_text() == "America/Montevideo\n"
    assert os.readlink(d / "etc/localtime") == "/usr/share/zoneinfo/America/Montevideo"
    assert "LANG=es_UY.UTF-8" in (d / "etc/default/locale").read_text()
    gen = (d / "etc/locale.gen").read_text().split("\n")
    assert "es_UY.UTF-8 UTF-8" in gen and "es_ES.UTF-8 UTF-8" in gen and "# es_AR.UTF-8 UTF-8" in gen
    assert ordenes == [["chroot", str(d), "locale-gen"]]
    # El teclado es el de la sesión, aunque el país sugiera otro
    assert 'XKBLAYOUT="latam"' in (d / "etc/default/keyboard").read_text()
    assert '"XkbLayout" "latam"' in (d / "etc/X11/xorg.conf.d/00-keyboard.conf").read_text()
    assert "KEYMAP=y" in (d / "etc/initramfs-tools/conf.d/tisma-teclado").read_text()


def test_region_sin_red_y_respuestas_hostiles(tmp_path):
    d, e = _destino(tmp_path), _sesion(tmp_path, "America/Argentina/Buenos_Aires")
    R.main([str(d)], abrir=_red(OSError("sin red")), run=lambda *a, **k: None, etc=str(e))
    assert (d / "etc/timezone").read_text() == "America/Argentina/Buenos_Aires\n"
    assert "es_AR.UTF-8" in (d / "etc/default/locale").read_text()
    for mala in ("../../../etc/shadow", "Europe/../../../../etc/passwd", "Europe/Nowhere", 42, "Europe/Madrid\n"):
        R.main([str(d)], abrir=_red({"time_zone": mala}), run=lambda *a, **k: None, etc=str(e))
        assert (d / "etc/timezone").read_text() == "America/Argentina/Buenos_Aires\n", mala
    # País no hispanohablante: formatos de España
    R.main([str(d)], abrir=_red({"time_zone": "Asia/Tokyo"}), run=lambda *a, **k: None, etc=str(e))
    assert "LANG=es_ES.UTF-8" in (d / "etc/default/locale").read_text()
