"""Construir desde cero: catálogo CC0 de build-your-own-x con piezas en español."""
from tisma_code import construir as c, kits, tools


def test_catalogo_completo_y_seguro():
    g = c.catalogo()
    assert len(g) >= 350 and all(x["url"].startswith(("https://", "http://")) for x in g)
    assert {x["tema"] for x in g} <= set(c.TEMAS)


def test_busca_en_espanol_con_piezas_y_lenguaje():
    r = c.buscar("quiero mi propio sistema operativo", "rust")
    assert r.startswith("## Sistema operativo") and "paginación" in r
    assert r.split("\n")[2].startswith("- [Rust]")


def test_sin_tema_lista_y_titulos_sueltos():
    assert "base de datos" in c.buscar("")
    assert "tetris" in c.buscar("tetris").lower() or "juego" in c.buscar("tetris").lower()
    assert c.buscar("zzzz-nada").startswith("Sin guías")


def test_kit_y_herramienta():
    assert "construir" in kits.from_text("diseña un emulador de chip-8 desde cero")
    assert "guias_construir" in tools.TOOL_NAMES
    assert "Piezas" in tools.execute(None, "guias_construir", {"tema": "servidor web"})
