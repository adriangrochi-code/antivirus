#!/usr/bin/env bash
# Arranca el kernel de TISMA en QEMU con un initramfs mínimo (busybox) y comprueba que las
# protecciones están ACTIVAS: pila de LSMs, lockdown, firmas obligatorias, endurecimiento.
# Uso: qemu-boot-test.sh <bzImage>
set -euo pipefail
KERNEL="${1:?bzImage}"
WORK="$(mktemp -d)"
trap 'rm -rf -- "${WORK:?}"' EXIT
mkdir -p "$WORK/root/bin" "$WORK/root/proc" "$WORK/root/sys" "$WORK/root/dev"
cp "$(command -v busybox)" "$WORK/root/bin/busybox"
# Planificador sched_ext opcional (SCX_BIN=ruta/a/scx_lavd): se copia con sus bibliotecas
if [[ -x "${SCX_BIN:-}" ]]; then
    cp "$SCX_BIN" "$WORK/root/bin/scx"
    for lib in $(ldd "$SCX_BIN" | grep -oE '/[^ ]+'); do
        mkdir -p "$WORK/root$(dirname "$lib")"; cp -L "$lib" "$WORK/root$lib"
    done
fi
# Prueba real del binder (Waydroid): binderfs + transacción entre procesos
gcc -O2 -static -o "$WORK/root/bin/binder-test" "$(dirname "${BASH_SOURCE[0]}")/binder-test.c"
cat > "$WORK/root/init" <<'INIT'
#!/bin/busybox sh
/bin/busybox --install -s /bin
mount -t proc proc /proc; mount -t sysfs sys /sys; mount -t devtmpfs dev /dev
mount -t securityfs none /sys/kernel/security 2>/dev/null
echo "== TISMA-TEST =="
echo "kernel=$(uname -r)"
echo "lsm=$(cat /sys/kernel/security/lsm)"
echo "lockdown=$(cat /sys/kernel/security/lockdown)"
echo "sig_enforce=$(cat /sys/module/module/parameters/sig_enforce 2>/dev/null)"
echo "init_on_free=$(grep -o 'init_on_free=1' /proc/cmdline)"
echo "devmem=$([ -e /dev/mem ] && echo presente || echo ausente)"
echo "kcore=$([ -e /proc/kcore ] && echo presente || echo ausente)"
echo "psi=$([ -e /proc/pressure/memory ] && echo si || echo no)"
echo "zswap=$(cat /sys/module/zswap/parameters/compressor 2>/dev/null)"
echo "mglru=$(cat /sys/kernel/mm/lru_gen/enabled 2>/dev/null)"
echo "binder=$(grep -c binder /proc/filesystems)"
/bin/binder-test
echo "rust_binder=$(grep -c ' rust_binder' /proc/kallsyms)"
echo "preempt=$(dmesg | grep -o 'Dynamic Preempt: [a-z]*' | tail -n1)"
echo "cpuidle=$(cat /sys/devices/system/cpu/cpuidle/current_governor 2>/dev/null)"
echo "rcu_offload=$(dmesg | grep -o 'Offload RCU callbacks from CPUs: [^.]*' | cut -d: -f2)"
echo "sched_ext=$([ -d /sys/kernel/sched_ext ] && echo si || echo no)"
if [ -x /bin/scx ]; then
    mount -t bpf bpf /sys/fs/bpf; mount -t debugfs none /sys/kernel/debug 2>/dev/null
    /bin/scx > /scx.log 2>&1 &
    i=0; while [ "$(cat /sys/kernel/sched_ext/state)" != enabled ] && [ $i -lt 120 ]; do sleep 1; i=$((i+1)); done
    echo "scx_activo=$(cat /sys/kernel/sched_ext/state) ops=$(cat /sys/kernel/sched_ext/root/ops 2>/dev/null)"
    # Carga: varios procesos a la vez con el planificador de Rust al mando
    for n in 1 2 3 4; do (j=0; while [ $j -lt 20000 ]; do j=$((j+1)); done) & done; wait %2 %3 %4 %5 2>/dev/null
    /bin/binder-test >/dev/null 2>&1; echo "scx_binder=$?"
    kill %1; i=0; while [ "$(cat /sys/kernel/sched_ext/state)" != disabled ] && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
    echo "scx_al_salir=$(cat /sys/kernel/sched_ext/state)"
    grep -iE "error|fail" /scx.log | head -n 3
fi
echo "vulns=$(grep -l Vulnerable /sys/devices/system/cpu/vulnerabilities/* 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')"
echo "== FIN =="
poweroff -f
INIT
chmod +x "$WORK/root/init"
(cd "$WORK/root" && find . | cpio -o -H newc --quiet | gzip > "$WORK/initrd.gz")
CMD="console=ttyS0 panic=-1 lsm=landlock,lockdown,yama,integrity,apparmor,bpf lockdown=integrity module.sig_enforce=1 init_on_alloc=1 init_on_free=1 page_alloc.shuffle=1 randomize_kstack_offset=on slab_nomerge vsyscall=none"
ACCEL=(); [[ -w /dev/kvm ]] && ACCEL=(-enable-kvm -cpu host)
timeout 300 qemu-system-x86_64 "${ACCEL[@]}" -m 1024 -smp 2 -nographic -no-reboot \
    -kernel "$KERNEL" -initrd "$WORK/initrd.gz" -append "$CMD" 2>&1 | tee "$WORK/out.txt" | grep -A30 "== TISMA-TEST ==" | sed '/== FIN ==/q'
fail=0
check() { grep -q "$1" "$WORK/out.txt" || { echo "FALLO: $2"; fail=1; }; }
check "lsm=.*landlock.*lockdown.*yama.*apparmor.*bpf" "pila de LSMs incompleta"
check "lockdown=.*\[integrity\]" "lockdown no está en modo integrity"
check "sig_enforce=Y" "firmas de módulos no obligatorias"
check "devmem=ausente" "/dev/mem accesible"
check "kcore=ausente" "/proc/kcore accesible"
check "psi=si" "PSI desactivado"
check "binder=[1-9]" "binderfs (Waydroid) ausente"
check "binder_prueba=ok" "el binder no completa una transacción"
check "rust_binder=[1-9]" "el binder no es la versión en Rust"
check "preempt=Dynamic Preempt: lazy" "preempción no está en modo lazy"
check "cpuidle=teo" "gobernador de reposo no es teo"
check "sched_ext=si" "sched_ext ausente"
if [[ -x "${SCX_BIN:-}" ]]; then
    check "scx_activo=enabled" "el planificador sched_ext no se activó"
    check "scx_al_salir=disabled" "al cerrar el planificador el kernel no volvió al suyo"
fi
[[ $fail == 0 ]] && echo "OK: el kernel arranca con todas las protecciones activas"
exit $fail
