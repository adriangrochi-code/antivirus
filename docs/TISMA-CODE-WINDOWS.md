# TISMA Code en Windows 10/11 (x64)

TISMA Code también funciona fuera de TISMA OS, como carpeta portable: no se instala nada ni hace falta Python.

## Conseguirlo y abrirlo

1. En GitHub: *Actions* → **TISMA Code (Windows)** → la última ejecución correcta → artefacto
   `tisma-code-windows-x64` (se descarga como `tisma-code-windows-x64.zip`). También se puede lanzar a mano
   (*Run workflow*).
2. Descomprime el ZIP donde quieras (por ejemplo en `Documentos`).
3. Doble clic en **`TISMA Code.vbs`** (sin consola) o en **`TISMA Code.cmd`** (con el registro a la vista). Se abre
   en una ventana propia de Edge (o Chrome; si no hay ninguno, en el navegador predeterminado). Al cerrar la ventana,
   TISMA Code y su motor se cierran.
4. Si la gráfica o su controlador Vulkan dan problemas: **`TISMA Code (solo CPU).cmd`**.

Para desarrollar: `python -m tisma_code` desde `userspace/tisma-code` (Python 3.12 o posterior; `llama-server.exe`
de llama.cpp tiene que estar en el `PATH`).

Modelos, configuración y memoria del proyecto se guardan en `%LOCALAPPDATA%\TISMA Code`. Para desinstalar, borra la
carpeta del ZIP y esa.

## Aviso de SmartScreen

El ZIP no está firmado, así que la primera vez Windows puede mostrar «Windows protegió su PC». Pulsa
**Más información → Ejecutar de todas formas**, solo si lo descargaste de las *Actions* del repositorio oficial.
`VERSION.txt` lista las versiones y los SHA-256 de todo lo que incluye (Python embebido, llama.cpp, sympy, mpmath).

## Qué cambia respecto a TISMA OS

En Windows no existe bubblewrap (la jaula que usa TISMA OS), así que **hay menos aislamiento**:

| | TISMA OS | Windows |
|---|---|---|
| Motor de IA (llama-server) | Jaula: sin carpeta personal, sistema de solo lectura | Job Object: muere con la app, no puede crear procesos, memoria acotada a la RAM, sin portapapeles. Escucha solo en 127.0.0.1 con clave aleatoria. **Podría leer tus archivos** si un modelo malicioso lograra ejecutar código: descarga modelos solo de fuentes de confianza |
| Comandos del agente (compilar, pruebas) | En la jaula, sin red | **Desactivados por defecto.** Se activan en *Ajustes → Comandos del proyecto*. Aun activados, **cada comando pide permiso** (sin «no volver a preguntar»), corren en `cmd.exe` con tiempo y memoria limitados, pero pueden leer y cambiar tus archivos y usar la red |
| Linters, formateadores, git, ripgrep | En la jaula | No se usan (algunos ejecutan código del repositorio); la búsqueda y el mapa del proyecto usan su versión interna |
| Cálculo exacto | Jaula + límites | Job Object (un proceso, 3 GiB, 60 s de CPU). El cálculo simbólico (sympy) solo corre si el propio proceso comprueba que está en ese Job |
| Reducir modelos | Jaula | Job Object (un proceso, memoria acotada) |
| REA, Craft Anything, SolidCraft, Simulación, Strata, Hope, OpenJarvis, «Sin apuro», componentes | Sí | No disponibles |

La interfaz lo indica sin nombrar el sistema («en este equipo»).

## Detalles técnicos

- Todo lo que en Linux va enjaulado se crea suspendido, se asigna a un Job Object con
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` y solo entonces se reanuda; si el Job no se puede crear, no se lanza
  (`tisma_code/plataforma.py`).
- El motor se considera propio solo si **todo** lo que escucha en su puerto es un proceso lanzado por TISMA Code
  (tabla TCP de Windows con el PID de cada puerto).
- Al cerrar, al motor se le manda Ctrl+Break para que termine en orden y después se cierra su Job.
  Pendiente de comprobar en un equipo real: si llama-server solo atiende Ctrl+C, no guardará su caché de n-gramas
  al cerrarse (no se pierde nada más).
- Pruebas: `tests/test_plataforma.py` (en cualquier sistema) y `tests/test_windows.py` (API reales de Win32, en la
  CI de Windows). Las pruebas que dependen de Linux no se recogen en Windows (`tests/conftest.py`).
