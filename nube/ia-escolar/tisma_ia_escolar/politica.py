"""Cómo responde la IA a un estudiante: tutor según su nivel, detección de riesgos y temas (sin guardar el texto).

Principios:
- Tutor, no máquina de hacer tareas: guía con preguntas y pistas, explica el porqué, y solo da la respuesta completa
  cuando el estudiante ya lo intentó (o cuando es una consulta de información, no una tarea).
- Adecuado a la edad: primaria (6-12) con frases cortas y ejemplos; liceo/UTU (12-18) con más profundidad.
- Seguridad de menores: nada sexual, ni violencia gráfica, ni cómo conseguir armas o drogas; no pide datos
  personales; ante señales de riesgo (autolesión, abuso, acoso) responde con cuidado, da las líneas de ayuda
  oficiales y avisa al referente de la escuela (solo la categoría y un resumen, nunca la conversación).
- Las reglas se aplican ANTES (entrada) y DESPUÉS (salida) del modelo: no dependen de que el modelo obedezca.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

NIVELES = {
    "primaria": {"edades": "6 a 12 años", "max_tokens": 500},
    "liceo": {"edades": "12 a 18 años", "max_tokens": 900},
}

# Líneas de ayuda de Uruguay, verificadas en fuentes oficiales (MSP, noviembre de 2026). Cada institución puede
# ajustarlas en la configuración. La Línea Azul de INAU NO se incluye hasta confirmar su número oficial.
AYUDA_UY = [
    "Línea de Prevención del Suicidio: 0800 0767, o *0767 desde el celular (gratis, las 24 horas)",
    "Línea de apoyo emocional: 0800 1920, o *1920 desde el celular (gratis, las 24 horas)",
    "Emergencias: 911",
]

BASE = """Sos el asistente de estudio de TISMA para estudiantes de {edades}, en una escuela de Uruguay. Hablás en \
español rioplatense, con calidez y respeto.
Tu tarea es AYUDAR A APRENDER, no hacer la tarea por el estudiante:
- Si es un ejercicio o una tarea, primero preguntá qué entendió o qué intentó, y guiá con pistas y preguntas paso a \
paso. Mostrá ejemplos parecidos en lugar de resolver el mismo. Si ya lo intentó y se trabó, explicá la solución \
completa y por qué es así.
- Si es una duda de información, explicá claro y con un ejemplo, y sugerí cómo comprobarlo.
- Cuando no sepas algo con seguridad, decilo. No inventes datos, fechas ni citas.
- Animá a consultar también con su docente y su familia.
Seguridad (siempre):
- Nunca pidas ni guardes datos personales (nombre completo, dirección, teléfono, cédula, fotos, contraseñas). Si el \
estudiante los comparte, decile con amabilidad que no hace falta y que no los comparta en internet.
- No hables de contenido sexual, violencia gráfica, armas, drogas, apuestas ni retos peligrosos; redirigí con \
amabilidad a algo apropiado para su edad.
- Si alguien cuenta que lo lastiman, que quiere hacerse daño o que está en peligro, respondé con calma y cariño, \
decile que no está solo/a, que hable YA con un adulto de confianza (familia, docente, adscripto) y dale estas líneas:
{ayuda}
- No sos una persona ni un amigo/a real: si lo preguntan, explicá que sos un programa de computadora."""

EXTRA = {
    "primaria": "Usá frases cortas y palabras simples, ejemplos de la vida diaria y emojis con moderación. Respuestas "
                "breves (pocas líneas) y de a un paso por vez.",
    "liceo": "Podés profundizar, usar notación matemática y vocabulario técnico explicándolo. Proponé cómo seguir "
             "practicando.",
}


def sistema(nivel: str, ayuda: list[str] | None = None) -> str:
    n = NIVELES[nivel]
    return BASE.format(edades=n["edades"], ayuda="\n".join(f"  · {a}" for a in (ayuda or AYUDA_UY))) + \
        "\n" + EXTRA[nivel]


# ---------------------------------------------------------------- riesgos
def normalizar(t: str) -> str:
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^\w\s@.+-]", " ", t)


_R = lambda *ps: re.compile("|".join(ps))  # noqa: E731

RIESGOS = {
    # Cuidado: se responde con apoyo y se avisa; NUNCA se bloquea (cortar la conversación en ese momento es peor)
    "autolesion": _R(r"\b(me quiero|quiero|ganas de|voy a|pienso en) (morir|matarme|suicidarme|desaparecer|"
                     r"cortarme|lastimarme|hacerme dano)", r"\bsuicid", r"\bno (quiero|tengo ganas de) (seguir )?"
                     r"(vivir|viviendo|existir|estar aca)", r"\bme (quiero|voy a) matar\b", r"\bme corto\b",
                     r"\bautolesion", r"\bmejor (estaria|estarian) (muerto|muerta|sin mi)",
                     r"\b(dormir|dormirme) y no despertar", r"\bno vale la pena (vivir|seguir)",
                     r"\bnadie me extranaria\b", r"\bquiero desaparecer\b"),
    "abuso": _R(r"\bme (pega|pegan|toca|tocan|obliga|obligan|amenaza|amenazan|lastima|lastiman)\b",
                r"\b(abus(o|an|aron|ó)|violaci|maltrat)", r"\bno le digas a nadie\b.*\b(toca|foto)",
                r"\bme pidio fotos\b", r"\bun adulto me (pide|pidio|escribe|manda)"),
    "acoso": _R(r"\bme (hacen|hacian) bullying\b", r"\bbullying\b", r"\bse burlan de mi\b",
                r"\bme (insultan|molestan|excluyen) (todos|siempre|en (el|la) (clase|liceo|escuela))"),
}
# Bloqueo: contenido que no corresponde a menores (se responde con una redirección, sin llamar al modelo)
BLOQUEO = {
    "sexual": _R(r"\b(porno|pornografia|xxx|desnud[oa]s?|nudes|sexo explicito|onlyfans)\b"),
    "armas_drogas": _R(r"\bcomo (hacer|fabricar|conseguir|comprar) (una )?(bomba|arma|pistola|explosivo|droga|"
                       r"cocaina|marihuana|pasta base)"),
    "retos": _R(r"\breto (de|del) (desmayo|asfixia|apnea)\b", r"\bblackout challenge\b"),
}

TEMAS = {
    "matematica": r"\b(suma|resta|multiplic|divisi|fraccion|ecuacion|porcentaje|geometri|triangulo|angulo|area|"
                  r"perimetro|algebra|funcion|derivad|integral|numero|calcul|matematic)",
    "lengua": r"\b(ortografia|acento|tilde|verbo|sustantivo|adjetivo|redaccion|cuento|poema|texto|lectura|"
              r"gramatica|sinonimo)",
    "ciencias": r"\b(celula|planta|animal|cuerpo humano|energia|fuerza|atomo|molecula|quimic|fisic|biolog|"
                r"ecosistema|planeta|universo|electricidad)",
    "sociales": r"\b(historia|artigas|independencia|guerra|civilizacion|geografia|pais|departamento|rio|"
                r"constitucion|democracia)",
    "ingles": r"\b(english|ingles|translate|traduc|vocabulary|grammar)",
    "programacion": r"\b(programa|codigo|python|scratch|arduino|variable|bucle|funcion en|algoritmo)",
    "arte": r"\b(dibujo|pintura|musica|cancion|instrumento|color)",
}
_TEMAS = {k: re.compile(v) for k, v in TEMAS.items()}

# Datos personales que se tapan antes de cualquier registro (y se avisa al estudiante)
DATOS = [(re.compile(r"\b\d\.?\d{3}\.?\d{3}-?\d\b"), "[cédula]"),
         (re.compile(r"\b0?9\d[\s-]?\d{3}[\s-]?\d{3}\b|\b2\d{3}[\s-]?\d{4}\b"), "[teléfono]"),
         (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[correo]")]


@dataclass
class Analisis:
    riesgo: str | None = None          # autolesion | abuso | acoso
    bloqueo: str | None = None         # sexual | armas_drogas | retos
    tema: str = "otros"
    datos_personales: bool = False
    motivos: list[str] = field(default_factory=list)


def analizar(texto: str) -> Analisis:
    n = normalizar(texto)
    a = Analisis()
    for cat, rx in RIESGOS.items():
        if rx.search(n):
            a.riesgo = cat
            break
    for cat, rx in BLOQUEO.items():
        if rx.search(n):
            a.bloqueo = cat
            break
    for tema, rx in _TEMAS.items():
        if rx.search(n):
            a.tema = tema
            break
    a.datos_personales = any(rx.search(texto) for rx, _ in DATOS)
    return a


def tapar_datos(texto: str) -> str:
    for rx, rep in DATOS:
        texto = rx.sub(rep, texto)
    return texto


REDIRECCION = {
    "sexual": "Eso no es un tema para hablar acá. Si tenés dudas sobre tu cuerpo o sobre algo que viste en internet, "
              "lo mejor es hablarlo con un adulto de confianza de tu familia o de la escuela. ¿Te ayudo con otra cosa?",
    "armas_drogas": "Con eso no te puedo ayudar, porque puede ser peligroso para vos o para otras personas. Si estás "
                    "pasando por una situación difícil, contáselo a un adulto de confianza. ¿Seguimos con otra cosa?",
    "retos": "Ese «reto» es muy peligroso: puede causar daños graves e incluso la muerte. Por favor no lo hagas, y si "
             "alguien te presiona para hacerlo, contáselo a un adulto de confianza.",
}


def pie_de_ayuda(ayuda: list[str] | None = None) -> str:
    return ("\n\nNo estás solo/a. Hablá ya con un adulto de confianza (tu familia, un docente o el adscripto). "
            "También podés llamar gratis:\n" + "\n".join(f"• {a}" for a in (ayuda or AYUDA_UY)))


def revisar_salida(texto: str, nivel: str) -> tuple[str, str | None]:
    """El modelo también se revisa: si su respuesta trae algo bloqueado, se reemplaza."""
    a = analizar(texto)
    if a.bloqueo:
        return REDIRECCION[a.bloqueo], a.bloqueo
    return texto, None
