# tisma-ranuras

Capa de **ranuras intercambiables** del kernel TISMA. Da la flexibilidad de un microkernel (cambiar una pieza sin
recompilar ni reiniciar) sin perder el rendimiento ni los drivers de Linux: cada ranura usa un mecanismo que Linux
ya trae, y esta herramienta los presenta a todos de la misma forma.

| Ranura | Mecanismo del kernel | Ejemplos de piezas |
|---|---|---|
| `planificador` | sched_ext | `eevdf` (el del kernel), `scx_lavd`, cualquier `scx_*` instalado |
| `frecuencia` | cpufreq | `schedutil`, `powersave`, `performance` |
| `reposo` | cpuidle | `teo`, `menu` |
| `disco` | blk-mq | `none`, `mq-deadline`, `bfq`, `kyber` (por disco) |
| `red` | control de congestión TCP | `cubic`, `bbr` |
| `compresion` | zswap | `zstd`, `lz4` |
| `memoria` | reclamación de memoria | `mglru`, `clasico` |
| `paginas` | páginas grandes de 2 MB (THP) | `madvise` (solo quien las pide), `always`, `never` |
| `paginas_medianas` | páginas medianas de 16 kB a 1 MB (mTHP) | `ninguna`, `64kB:always`, `64kB:always+128kB:inherit` |

Las páginas grandes y medianas reducen los fallos de la TLB (la tabla de traducción de direcciones de la CPU):
menos ciclos esperando a la memoria, más IPC en programas que mueven mucha memoria (IA local, compilación,
juegos). A cambio pueden ocupar algo más de RAM; por eso cada perfil de equipo elige. En `paginas_medianas`, los
tamaños que no se nombran quedan apagados, y `inherit` sigue lo que diga `paginas`.

```sh
tisma-ranuras listar                       # ranura, pieza actual, piezas disponibles, descripción (tabuladores)
tisma-ranuras cambiar planificador scx_lavd
tisma-ranuras aplicar perfil.txt           # líneas «ranura=pieza»; todo o nada
tisma-ranuras revertir                     # vuelve a lo que había antes del primer cambio
```

Garantías:

- **Vocabulario cerrado:** las piezas válidas son las que ofrece el kernel en ese momento; los planificadores,
  solo programas `scx_[a-z0-9_]+` de `/usr/bin` o `/usr/sbin`. No acepta rutas.
- **Verificación:** cada cambio se lee de vuelta. El planificador debe tomar el control y seguir activo; si no,
  se detiene y el kernel vuelve solo a `eevdf` (sched_ext también lo expulsa si se porta mal).
- **Reversible:** si un cambio falla se deja lo anterior; `aplicar` deshace todo si falla una línea; el estado
  original queda en `/run/tisma-ranuras/anterior` (se pierde al reiniciar: el arranque siempre es el de fábrica).
- **Seguridad intacta:** ninguna ranura toca mitigaciones de CPU, lockdown, firma de módulos ni LSM.

Rust, sin dependencias externas. Pruebas: `cargo test` (sobre un /sys falso) y, en la VM con el kernel TISMA,
`RANURAS_BIN=… SCX_BIN=… tests/qemu-boot-test.sh <bzImage>`.

Compilación estática (para el initramfs de prueba):
`RUSTFLAGS="-C target-feature=+crt-static" cargo build --release --target x86_64-unknown-linux-gnu`
