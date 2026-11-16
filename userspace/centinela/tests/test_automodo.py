"""Modo automático: poca batería → Eco recordando el modo; al enchufar vuelve; lo manual manda."""
from centinela.automodo import decidir


def test_reglas():
    assert decidir({"perfil": "rendimiento"}, False, 10)[0] is None                    # desactivado
    p = {"auto_modo": True, "perfil": "rendimiento"}
    nuevo, p = decidir(p, False, 30)
    assert nuevo is None                                                              # todavía hay batería
    nuevo, p = decidir(p, False, 18)
    assert nuevo == "silencioso" and p["auto_previo"] == "rendimiento"
    nuevo, p = decidir(p, False, 12)
    assert nuevo is None                                                              # ya en Eco
    nuevo, p = decidir(p, True, 12)
    assert nuevo == "rendimiento" and "auto_previo" not in p
    nuevo, p = decidir({"auto_modo": True, "perfil": "silencioso"}, False, 5)
    assert nuevo is None                                                              # ya estaba en Eco
