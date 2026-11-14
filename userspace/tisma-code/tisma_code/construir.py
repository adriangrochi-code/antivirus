"""Construir desde cero: guías paso a paso para que el modelo diseñe «lo que sea».

Catálogo de build-your-own-x (CodeCrafters y colaboradores, CC0: dominio público): 360 tutoriales para
construir tu propio sistema operativo, base de datos, navegador, juego, lenguaje… en muchos lenguajes.
Son enlaces (leerlos necesita la red, que se permite por proyecto); sin red, el modelo igual tiene las PIEZAS de
cada tema (escritas para TISMA) para planificar el diseño y el orden en que conviene construirlo.
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

ARCHIVO = Path(__file__).with_name("construir_guias.json")

# tema original → (nombre en español, sinónimos para buscar, piezas en el orden sugerido)
TEMAS = {
    "3D Renderer": ("motor de render 3D", "render 3d raytracing trazado de rayos rasterizado graficos opengl",
                    "matemática de vectores y matrices → cámara y proyección → rasterizado de triángulos o trazado "
                    "de rayos → profundidad (z-buffer) → luces y sombreado → texturas → antialiasing"),
    "AI Model": ("modelo de IA", "ia llm transformer gpt modelo de lenguaje inteligencia artificial",
                 "tokenizador → embeddings → atención → bloque transformer → entrenamiento (pérdida, optimizador) → "
                 "muestreo de texto → evaluación"),
    "Augmented Reality": ("realidad aumentada", "ar realidad aumentada camara marcadores",
                          "captura de cámara → detección de marcadores o rasgos → estimación de pose → "
                          "proyección del objeto 3D → seguimiento cuadro a cuadro"),
    "BitTorrent Client": ("cliente BitTorrent", "bittorrent torrent p2p descargas",
                          "lector bencode → archivo .torrent y hash → tracker → protocolo entre pares (handshake, "
                          "mensajes) → piezas y verificación SHA-1 → descarga en paralelo"),
    "Blockchain / Cryptocurrency": ("cadena de bloques / criptomoneda", "blockchain cripto bitcoin cadena de bloques",
                                    "bloque y hash → cadena y validación → prueba de trabajo → transacciones y "
                                    "firmas → red entre nodos → consenso (la cadena más larga)"),
    "Bot": ("bot", "bot chatbot telegram discord slack",
            "conexión con la plataforma (API) → recibir mensajes → interpretar órdenes → responder → estado y "
            "memoria → despliegue"),
    "Command-Line Tool": ("herramienta de línea de comandos", "cli consola comando terminal herramienta",
                          "argumentos y ayuda → lógica principal → entrada y salida (archivos, tuberías) → errores "
                          "y códigos de salida → pruebas → empaquetado"),
    "Database": ("base de datos", "base de datos sql sqlite redis clave valor almacenamiento",
                 "formato en disco por páginas → árbol B o LSM → analizador SQL → planificador y ejecutor → "
                 "índices → transacciones y registro (WAL) → recuperación tras fallo"),
    "Distributed Systems": ("sistemas distribuidos", "distribuido raft paxos consenso cluster replicacion",
                            "red y mensajes → replicación → elección de líder (Raft) → registro replicado → "
                            "tolerancia a fallos → pruebas con fallos simulados"),
    "Docker": ("contenedores (tipo Docker)", "docker contenedor container namespaces cgroups aislamiento",
               "namespaces (procesos, red, montajes) → raíz propia (chroot/pivot_root) → cgroups (límites) → "
               "imagen por capas → red del contenedor"),
    "Emulator / Virtual Machine": ("emulador / máquina virtual", "emulador maquina virtual chip-8 gameboy nes vm",
                                   "memoria → registros → ciclo leer-decodificar-ejecutar → instrucciones → "
                                   "pantalla y entrada → temporizadores y sonido → pruebas con ROM de test"),
    "Front-end Framework / Library": ("biblioteca de interfaz web", "frontend react vue dom virtual componentes framework",
                                      "componentes → DOM virtual → comparación y parcheo (diff) → estado y "
                                      "reactividad → eventos → enrutado"),
    "Game": ("juego", "juego videojuego game tetris snake plataformas arcade",
             "bucle del juego (entrada, actualizar, dibujar) → estados y pantallas → física y colisiones → "
             "niveles → sonido → puntuación y guardado"),
    "Git": ("control de versiones (tipo Git)", "git control de versiones vcs commits",
            "objetos con hash (blob, árbol, commit) → índice → commit → ramas y referencias → diff → fusión"),
    "Memory Allocator": ("gestor de memoria", "malloc asignador memoria allocator heap",
                         "pedir memoria al sistema → bloques y cabeceras → lista libre → partir y unir bloques → "
                         "alineación → pruebas de estrés"),
    "Network Stack": ("pila de red", "tcp ip red pila protocolos ethernet",
                      "dispositivo TAP → Ethernet y ARP → IPv4 → ICMP (ping) → UDP → TCP (conexión, ventanas, "
                      "retransmisión)"),
    "Neural Network": ("red neuronal", "red neuronal perceptron backpropagation aprendizaje",
                       "neurona y capas → propagación hacia adelante → función de pérdida → retropropagación → "
                       "entrenamiento por lotes → evaluación"),
    "Operating System": ("sistema operativo", "sistema operativo kernel nucleo os bootloader",
                         "arranque → modo protegido/largo → pantalla y teclado → interrupciones → memoria "
                         "(paginación) → procesos y planificador → sistema de archivos → llamadas al sistema"),
    "Physics Engine": ("motor de física", "fisica colisiones rigidos simulacion",
                       "vectores → integración del movimiento → detección de colisiones (amplia y fina) → "
                       "respuesta a colisiones → restricciones y uniones → paso de tiempo estable"),
    "Processor": ("procesador", "cpu procesador risc-v verilog fpga",
                  "juego de instrucciones → registros y ALU → decodificador → memoria → camino de datos → "
                  "segmentación (pipeline) → pruebas en simulador o FPGA"),
    "Programming Language": ("lenguaje de programación", "lenguaje interprete compilador parser lisp",
                             "analizador léxico → analizador sintáctico (árbol) → intérprete o generación de "
                             "código → entorno y variables → funciones y cierres → errores → biblioteca básica"),
    "Regex Engine": ("motor de expresiones regulares", "regex expresiones regulares automata",
                     "analizar el patrón → autómata (NFA) → búsqueda con retroceso o simulación → clases y "
                     "repeticiones → grupos"),
    "Search Engine": ("buscador", "buscador motor de busqueda indice invertido",
                      "rastreo o carga de documentos → tokenizado → índice invertido → consulta → ranking "
                      "(TF-IDF/BM25) → resultados"),
    "Shell": ("intérprete de órdenes (shell)", "shell bash terminal interprete de ordenes",
              "leer línea → separar palabras → ejecutar programas (fork/exec) → órdenes internas → tuberías y "
              "redirecciones → señales y trabajos"),
    "Template Engine": ("motor de plantillas", "plantillas template html jinja",
                        "analizar la plantilla → variables → condiciones y bucles → herencia → escape seguro de "
                        "HTML"),
    "Text Editor": ("editor de texto", "editor de texto vim nano",
                    "terminal en modo crudo → mostrar texto → mover el cursor → editar → guardar → buscar → "
                    "resaltado de sintaxis"),
    "Visual Recognition System": ("reconocimiento de imágenes", "vision reconocimiento imagenes rostros ocr camara",
                                  "carga de imágenes → preprocesado → rasgos o red convolucional → "
                                  "clasificación → evaluación con datos de prueba"),
    "Voxel Engine": ("motor de vóxeles", "voxel minecraft cubos mundo",
                     "bloques en trozos (chunks) → malla visible → cámara → generación del terreno → "
                     "iluminación → guardar el mundo"),
    "Web Browser": ("navegador web", "navegador browser html css render",
                    "red y HTTP → analizar HTML (DOM) → analizar CSS → estilos → disposición (layout) → "
                    "pintado → enlaces e historial"),
    "Web Server": ("servidor web", "servidor web http server api",
                   "socket y escucha → analizar peticiones HTTP → rutas → respuestas y archivos → concurrencia → "
                   "seguridad básica (rutas, tamaños)"),
    "Uncategorized": ("otros", "otros varios", ""),
}

SCHEMAS = [{"type": "function", "function": {
    "name": "guias_construir",
    "description": "Para diseñar o construir algo desde cero (sistema operativo, base de datos, juego, "
                   "navegador, lenguaje, emulador, red neuronal, servidor web, shell, Git, Docker, blockchain…): "
                   "da las piezas en el orden sugerido y guías paso a paso (en inglés) por lenguaje. Sin tema, "
                   "lista los temas.",
    "parameters": {"type": "object", "properties": {
        "tema": {"type": "string", "description": "qué se quiere construir, p. ej. 'base de datos'"},
        "lenguaje": {"type": "string", "description": "opcional, p. ej. Python, Rust, C, JavaScript"}}}}}]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


@lru_cache(maxsize=1)
def catalogo() -> list[dict]:
    return json.loads(ARCHIVO.read_text(encoding="utf-8"))["guias"]


def temas(tema: str) -> list[str]:
    """Temas que coinciden, del mejor al peor."""
    q = set(re.findall(r"\w+", _norm(tema)))
    puntos = []
    for k, (es, sin, _) in TEMAS.items():
        palabras = set(re.findall(r"\w+", _norm(f"{k} {es} {sin}")))
        p = len(q & palabras) + (3 if _norm(es) in _norm(tema) or _norm(k) in _norm(tema) else 0)
        if p:
            puntos.append((p, k))
    return [k for _, k in sorted(puntos, key=lambda x: -x[0])]


def buscar(tema: str = "", lenguaje: str = "", maximo: int = 12) -> str:
    if not tema.strip():
        return "Temas: " + ", ".join(v[0] for k, v in TEMAS.items() if k != "Uncategorized") + "."
    encontrados = temas(tema)
    guias = catalogo()
    if not encontrados:                               # buscar en los títulos (p. ej. «tetris», «chip-8»)
        q = _norm(tema)
        sueltas = [g for g in guias if q in _norm(g["titulo"])]
        if not sueltas:
            return f"Sin guías para «{tema}». " + buscar()
        return _formato("", sueltas, lenguaje, maximo)
    salida = []
    for k in encontrados[:2]:
        es, _, piezas = TEMAS[k]
        cab = f"## {es[0].upper() + es[1:]}" + (f"\nPiezas, en este orden: {piezas}." if piezas else "")
        salida.append(_formato(cab, [g for g in guias if g["tema"] == k], lenguaje, maximo))
    return "\n\n".join(salida) + "\n\n(Guías de build-your-own-x, dominio público. Leerlas necesita la red del " \
        "proyecto; sin red, diseña con las piezas y verifica cada una con pruebas antes de seguir.)"


def _formato(cabecera: str, guias: list[dict], lenguaje: str, maximo: int) -> str:
    if lenguaje:
        l = _norm(lenguaje)
        propias = [g for g in guias if l in _norm(g["lenguaje"])]
        guias = propias + [g for g in guias if g not in propias] if propias else guias
    lineas = [f"- [{g['lenguaje']}] {g['titulo']} — {g['url']}" for g in guias[:maximo]]
    resto = len(guias) - maximo
    return "\n".join(filter(None, [cabecera, *lineas, f"(y {resto} más)" if resto > 0 else ""]))
