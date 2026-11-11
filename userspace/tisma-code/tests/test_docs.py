import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code.docs import DocSpace  # noqa: E402


@pytest.fixture
def space(tmp_path):
    lib = tmp_path / "biblio"
    (lib / "manuales").mkdir(parents=True)
    (lib / "manuales" / "motor.md").write_text("# Motor M200\n\nCorriente nominal 7,7 A a 230 V.\n\n"
                                               "Par de arranque: 2,4 N·m.\n")
    return DocSpace(tmp_path / "proj", tmp_path / "idx", library=lib)


def test_search_and_read(space):
    hits = space.search("corriente nominal del motor")
    assert hits and hits[0]["ref"] == "biblioteca:manuales/motor.md" and "«" in hits[0]["snippet"]
    assert "Par de arranque" in space.read(hits[0]["ref"])


def test_save_is_indexed_and_readable(space):
    ref = space.save("descubrimiento", "Causa del fallo de cos φ", "La función ignoraba el factor de potencia.")
    assert ref.startswith("proyecto:descubrimientos/")
    assert any(h["ref"] == ref for h in space.search("factor de potencia"))
    assert "Tipo: descubrimiento" in space.read(ref)


def test_refresh_detects_changes_and_deletions(space):
    p = space.library / "manuales" / "motor.md"
    p.write_text("# Motor M200\n\nVelocidad 2850 rpm\n")
    assert space.search("rpm")
    p.unlink()
    assert not space.search("rpm")


def test_confinement(space):
    with pytest.raises(ValueError):
        space.read("biblioteca:../../etc/passwd")
    with pytest.raises(ValueError):
        space.save("virus", "x", "y")
