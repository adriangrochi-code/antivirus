"""Todo ajuste de Centinela es reversible: «centinela fabrica» vuelve a los valores de Debian."""
from centinela import apply as ap
from centinela import cli
from centinela.tuner import make_plan
from tests.test_tuner import HW


def test_fabrica_quita_solo_lo_suyo_y_no_vuelve_a_aplicar(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ap, "ETC", str(tmp_path / "etc"))
    monkeypatch.setattr(ap, "STATE_DIR", str(tmp_path / "state"))
    escritos = ap.persist(make_plan(HW, {}, "escritorio", "equilibrado"))
    ajeno = tmp_path / "etc/sysctl.d/99-mio.conf"
    ajeno.write_text("vm.swappiness = 10\n")
    hecho_a_mano = tmp_path / "etc/udev/rules.d/60-centinela-io.rules"
    quitados = ap.fabrica()
    assert set(quitados) == {p for p in escritos if p.endswith((".conf", ".rules", ".cfg", ".json", ".txt",
                                                                ".config"))}
    assert ajeno.exists() and not hecho_a_mano.exists() and ap.en_fabrica()
    assert cli.main(["apply", "--dry-run"]) == 0 and "valores de fábrica" in capsys.readouterr().out
    ap.reactivar()
    assert not ap.en_fabrica()


def test_build_kernel_solo_en_modo_dev():
    assert cli.main(["build-kernel"]) == 2
