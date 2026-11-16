#!/usr/bin/env bash
# Reparto de CPU entre primer plano y fondo con cada planificador, en una VM de 2 CPU:
# tareas de primer plano + 2 de fondo (en un cgroup «background.slice»), todas al 100 % durante N segundos.
#   2 de primer plano: con eevdf (el de Linux) y scx_bpfland se reparten a partes iguales; con scx_tisma el
#     primer plano debería quedarse casi toda la CPU (el fondo, lo que sobra: casi nada).
#   1 de primer plano: queda una CPU libre; con scx_tisma el fondo debería aprovecharla entera (50 %/50 %),
#     mientras que eevdf reparte entre las 3 (el primer plano baja a ~33 %).
# Uso: tests/planificador-test.sh <bzImage> <.config del kernel> <scx_bpfland> <scx_tisma> [segundos]
set -euo pipefail
KERNEL=$1 CONFIG=$2 BPFLAND=$3 TISMA=$4 SEG=${5:-8}
W="$(mktemp -d)"
trap 'rm -rf -- "${W:?}"' EXIT
R="$W/root"
mkdir -p "$R"/{bin,proc,sys,dev,boot,usr/bin,run}
cp "$(command -v busybox)" "$R/bin/busybox"
cp "$CONFIG" "$R/boot/config-$(file -bL "$KERNEL" | grep -oP 'version \K[^ ]+')"
for b in "$BPFLAND" "$TISMA"; do
    cp "$b" "$R/usr/bin/"
    for lib in $(ldd "$b" | grep -oE '/[^ ]+'); do mkdir -p "$R$(dirname "$lib")"; cp -L "$lib" "$R$lib"; done
done
cat > "$R/init" <<INIT
#!/bin/busybox sh
/bin/busybox --install -s /bin
mount -t proc proc /proc; mount -t sysfs sys /sys; mount -t devtmpfs dev /dev
mount -t cgroup2 none /sys/fs/cgroup; mount -t bpf bpf /sys/fs/bpf; mount -t tracefs none /sys/kernel/tracing 2>/dev/null
mkdir -p /sys/fs/cgroup/background.slice
SEG=$SEG
ticks() { awk '{print \$14+\$15}' /proc/\$1/stat; }
medir() {
    nombre=\$1; nfg=\$2; shift 2
    if [ -n "\$1" ]; then
        "\$@" > /run/sched.log 2>&1 &
        SP=\$!
        i=0; while [ "\$(cat /sys/kernel/sched_ext/state)" != enabled ] && [ \$i -lt 240 ]; do sleep 0.5; i=\$((i+1)); done
    fi
    pp1=""; pp2=""
    for n in \$(seq 1 \$nfg); do (while :; do :; done) & eval pp\$n=\\\$!; done
    for n in 1 2; do (while :; do :; done) & eval fp\$n=\\\$!; done
    echo \$fp1 > /sys/fs/cgroup/background.slice/cgroup.procs
    echo \$fp2 > /sys/fs/cgroup/background.slice/cgroup.procs
    sleep 2   # el planificador revisa los cgroups cada segundo
    t() { [ -n "\$1" ] && ticks \$1 || echo 0; }
    a1=\$(t "\$pp1"); a2=\$(t "\$pp2"); b1=\$(ticks \$fp1); b2=\$(ticks \$fp2)
    sleep \$SEG
    pp=\$(( \$(t "\$pp1") - a1 + \$(t "\$pp2") - a2 ))
    fp=\$(( \$(ticks \$fp1) - b1 + \$(ticks \$fp2) - b2 ))
    total=\$((pp + fp)); [ \$total -gt 0 ] || total=1
    echo "RESULTADO \$nombre (\$nfg de primer plano + 2 de fondo, 2 CPU) primer_plano=\$((100 * pp / total))% fondo=\$((100 * fp / total))% estado_scx=\$(cat /sys/kernel/sched_ext/state)"
    kill \$pp1 \$pp2 \$fp1 \$fp2 2>/dev/null
    if [ -n "\${SP:-}" ]; then kill \$SP; wait \$SP 2>/dev/null; grep -iE "fondo:|error" /run/sched.log | sed 's/^/  /'; SP=""; fi
    sleep 1
}
for nfg in 2 1; do
    medir eevdf \$nfg
    medir scx_bpfland \$nfg /usr/bin/scx_bpfland
    medir scx_tisma \$nfg /usr/bin/scx_tisma
done
poweroff -f
INIT
chmod +x "$R/init"
(cd "$R" && find . | cpio -o -H newc --quiet | gzip > "$W/initrd.gz")
ACCEL=(); TSC=""; [[ -w /dev/kvm ]] && ACCEL=(-enable-kvm -cpu host)
timeout 900 qemu-system-x86_64 "${ACCEL[@]}" -m 1024 -smp 2 -nographic -no-reboot -kernel "$KERNEL" \
    -initrd "$W/initrd.gz" -append "console=ttyS0 panic=-1 quiet" 2>&1 | tr -d '\r' | grep -E "RESULTADO|fondo:|error" | sed 's/\x1b\[[0-9;]*m//g'
