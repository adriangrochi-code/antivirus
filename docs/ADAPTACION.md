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
| Sin overclock ni undervolt (límite duro §5) | `centinela oc` / `autotune` existen | **Choca con §5**: quedan fuera de los modos y de la ISO por defecto hasta decidirlo |
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
# Línea base afinada: lo mismo con TLP y thermald instalados (etiqueta debian-tlp), y después quitarlos
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
| Memoria escasa | — | `systemd-run -p MemoryMax=4G …` o arrancar con `mem=4G` (pendiente: tarea `memoria`) |
| Ancho de banda de 1 canal | Medido (1/2/4 hilos) | Igual, si también son de 1 canal |
| RAPL / amd-pstate | Probablemente no hay (Excavator): el inventario lo confirma | Según CPU |

## 4. Próximos pasos (en este orden)

1. Correr el banco en la A275 y publicar la tabla (sin esto no se cambia ningún valor por defecto).
2. Formato del perfil firmado (Ed25519, por DMI + CPU) con esquema validable y anti-retroceso.
3. `tisma-adapt`: clases de carga con histéresis (sobre `adaptd`), un ajuste a la vez con prueba y
   reversión, contador de arranques fallidos y lista negra por modelo.
4. Tablas de la sección 4 dentro del perfil, verificando que cada ruta exista en el equipo.
5. Prototipos opcionales: ECC «Verificar» con inyección de fallos y pasos 2 y 3 del experimento 6.2.
