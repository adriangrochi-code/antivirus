# scx_tisma: planificador propio de TISMA

Planificador `sched_ext` (se carga en marcha, sin recompilar el kernel; si falla o se cierra, el kernel vuelve
solo a su planificador). Parte de **scx_bpfland** de Andrea Righi (sched-ext/scx v1.1.4, GPL-2.0-only), que ya
prioriza lo interactivo y reparte primero entre núcleos completos: en el A12 del A275 Linux ve las parejas de
núcleos que comparten FPU y L2 como «hermanos», así que los hilos pesados se separan solos.

**Lo que añade TISMA: la clase de fondo.** Las tareas de los cgroups de fondo (`background.slice` de systemd,
`tisma-fondo.slice`, configurable con `--fondo-cgroup`) van a una cola aparte que solo se atiende cuando ninguna
CPU tiene trabajo de primer plano:

- cuando llega trabajo de primer plano y no hay CPU libre, se le quita la CPU a una tarea de fondo al instante;
- una CPU que queda libre se aprovecha para el fondo (no se desperdicia);
- una tarea de fondo que espera más de 250 ms (`--fondo-espera-ms`) pasa delante una vez: nada se queda sin
  ejecutarse y el vigilante de sched_ext (5 s) nunca expulsa al planificador.

Así, lo que el usuario usa (juego, IA, navegador) conserva toda la CPU mientras corren actualizaciones,
indexado, copias o el análisis previo de Escudo. Medido en VM de 2 CPU (`tests/planificador-test.sh`), todas
las tareas al 100 %:

| Situación | Linux (eevdf) | scx_bpfland | scx_tisma |
|---|---|---|---|
| 2 primer plano + 2 fondo | 50 % / 50 % | 50 % / 50 % | 99 % / ~0 % |
| 1 primer plano + 2 fondo | 33 % / 66 % | 33 % / 66 % | 50 % / 50 % |

(primer plano / fondo, porcentaje del tiempo de CPU total)

Uso: `tisma-ranuras cambiar planificador scx_tisma` (instalado en `/usr/bin`). Compilar: `./compilar.sh`
(clona sched-ext/scx en el commit fijado, copia `scx_tisma/` dentro y compila con clang ≥ 20).
