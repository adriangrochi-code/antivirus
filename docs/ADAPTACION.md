# Adaptación medida: de «compilar el kernel» a «generar configuración»

Respuesta al documento de traspaso «rendimiento por vatio, modos Eco/Normal/Máximo, compresión y ECC opcional».
Regla de oro del documento: **nada se incorpora por defecto sin una mejora repetible frente a la línea base
afinada** (Debian con TLP y thermald), medida con `centinela banco`.

## 1. Lo que ya hay y cómo encaja

| Pieza del documento | Ya existe | Estado |
|---|---|---|
| Kernel de Debian, `build-kernel` solo `--dev` | `centinela build-kernel` exige `--dev` | Hecho |
| Configuración generada (sysctl, udev, systemd) | `tuner.make_plan` → `apply.persist` (sysctl.d, udev, oomd, memlock, zram, GRUB) | Hecho; falta firmarla por modelo |
| Lista blanca de rutas `/proc` y `/sys` | `apply.Actuator.WRITABLE` (segmentos, sin `..`) | Hecho |
| Reversible a Debian | `centinela fabrica` / `centinela apply --reactivar` | Hecho |
| Aplicar, probar y revertir si empeora | `centinela comparar probar <ajuste>` (rondas A-B-B-A, prueba de permutación) | Hecho para ajustes sueltos; falta usarlo en cada ajuste nuevo del perfil |
| Clases de carga dinámicas | `adaptd`: estados normal / juego / presión, modo IA (pausa KSM y compactación) | Parcial: faltan interactivo, multimedia, lote, fondo, inactivo |
| Slices de systemd | `tisma-segundo-plano.slice` (mantenimiento, escaneos) | Parcial |
| Modos Eco / Normal / Máximo | perfiles `silencioso` / `equilibrado` / `rendimiento` (+ batería: `energia.py`) | Nombres hechos; faltan las tablas por clase de carga |
| Límite térmico | `calor.py` (registro de frenado), `adaptd.thermal_guard` (overclock de GPU) | Parcial: falta «Máximo vuelve a Normal si la temperatura sostenida supera el techo» |
| Sin overclock ni undervolt (límite duro §5) | Ningún modo lo hace (antes «Máximo» sugería +10 % de GPU) | Decidido: el overclock manual de TISMA Rendimiento y `autotune` quedan como herramienta manual aparte, con aviso; ningún modo los usa |
| ECC pasivo (§6.1) | `salud-hardware` (EDAC/MCE vía rasdaemon, SMART, btrfs, pstore), scrub mensual | Hecho; falta prueba de RAM al instalar |
| Banco de pruebas (§8) | `centinela banco` (este cambio) | Hecho lo medible sin ventanas |

## 2. Banco de pruebas: cómo correrlo en la A275

Siempre a batería (si no, no hay vatios), con el brillo fijo y la misma temperatura de partida (el banco espera
a que se enfríe entre repeticiones).

```sh
sudo centinela banco inventario --salida inventario-a275.json      # sección 2: confirma CMT, RAPL, EDAC, TPM…
# Línea base: Debian sin ajustes de TISMA
sudo centinela fabrica && sudo reboot
sudo centinela banco correr --etiqueta debian --repeticiones 5 --csv a275.csv
sudo centinela banco correr reposo --minutos 10 --etiqueta debian --csv a275.csv
sudo centinela banco correr inferencia compresion_modelo --modelo ~/.local/share/tisma-code/models/…/x.gguf \
     --etiqueta debian --csv a275.csv
# Línea base afinada: TLP y thermald se instalan y activan solos (thermald solo corre en Intel)
sudo centinela banco preparar debian-tlp
sudo centinela banco correr --etiqueta debian-tlp --repeticiones 5 --csv a275.csv     # (y reposo, inferencia…)
sudo centinela banco preparar tisma          # los quita (solo si los instaló el banco)
# TISMA en cada modo
sudo centinela apply --reactivar && sudo reboot
# (elegir Eco en Preferencias) … --etiqueta tisma-eco ; (Normal) … tisma-normal ; (Máximo) … tisma-maximo
centinela banco tabla a275.csv                                     # media ± desviación y % frente a la base
```

Tareas incluidas: `reposo`, `ancho_banda` (1, 2 y 4 hilos), `descompresion` (lz4 y zstd, 1 y 4 procesos),
`compresion_modelo` (paso 1 del experimento 6.2: razón extra de zstd -19 y xz -9 sobre el GGUF; si es menor que
1,1, el experimento se descarta), `almacenamiento` (fio 4K aleatorio, profundidad 1 y 32), `arranque`,
`escritorio` (p99 del temporizador con la CPU llena), `compilacion` (fuentes fijas) e `inferencia`
(llama-bench en la jaula del motor). Pendientes, porque necesitan un guion con ventanas: navegación,
multimedia y juegos.

## 3. Medido en la A275 frente a simulado

| Qué | En la A275 | Equipos de 4 a 8 GB (Ceibal) |
|---|---|---|
| Todo lo del banco | Medido | — |
| Memoria escasa | — | Tarea `memoria`: carga de 3 y 5 GB dentro de un límite de 4 GB (systemd-run MemoryMax): trabajo, presión «full», intercambio y cierres por falta de memoria. Para una prueba completa del escritorio, arrancar con `mem=4G` |
| Ancho de banda de 1 canal | Medido (1/2/4 hilos) | Igual, si también son de 1 canal |
| RAPL / amd-pstate | Probablemente no hay (Excavator): el inventario lo confirma | Según CPU |

## 4. Próximos pasos (en este orden)

1. Correr el banco en la A275 y publicar la tabla (sin esto no se cambia ningún valor por defecto).
2. ~~Formato del perfil firmado~~ Hecho: `centinela/perfil.py`, esquema en `docs/perfil.schema.json`, ejemplo
   (hipótesis sin medir ni firmar) en `docs/perfiles/thinkpad-a275.hipotesis.json`. Vocabulario cerrado de
   ajustes con sus valores permitidos, límites duros (mitigaciones siempre «auto», techo de temperatura,
   memoria reservada), variante agresiva solo con ajustes marcados como seguros, lista negra y versión
   anti-retroceso. `centinela perfil validar|firmar|verificar|esquema`. Claves públicas de confianza en
   `/usr/share/tisma/perfiles/claves/*.pem`; la privada se crea y guarda en el equipo de quien publica
   (`openssl genpkey -algorithm ed25519`), nunca en el repositorio.
3. ~~`tisma-adapt`~~ Hecho (`centinela/adapt.py`, dentro de `adaptd`; **solo actúa con un perfil firmado y
   válido del equipo**: hoy no se distribuye ninguno, así que no cambia nada): clases de carga con histéresis
   (declarado → señales → heurística), un ajuste nuevo a la vez con ventana de prueba y reversión ante errores
   del kernel de su componente (lista negra local), Máximo baja a Normal con temperatura sostenida sobre el
   techo, y contador de arranques: dos seguidos que no llegan a 3 minutos suspenden el perfil
   (`centinela adapt estado|reanudar`). Lo que no es de /sys (hilos de IA, zram, planificador, fondo) se publica
   en `/run/centinela/politica.json`.
4. Tablas de la sección 4 dentro del perfil, verificando que cada ruta exista en el equipo.
5. ~~ECC «Verificar»~~ Hecho, opcional y desactivado por defecto (`centinela/ecc.py`; TISMA Rendimiento →
   «Protección de memoria por software»): huellas BLAKE2b por bloque de 4 MiB de los modelos de IA, revisión cada
   30 min a prioridad mínima y nunca mientras la IA genera, recarga desde el disco si un bloque no coincide y aviso
   a TISMA Code si el motor tiene el modelo en uso. `centinela ecc prueba` mide detección y velocidad con fallos
   inyectados. No sustituye a la memoria con ECC.
6. Pasos 2 y 3 del experimento 6.2 (compresión del modelo): esperan los datos de la A275.
