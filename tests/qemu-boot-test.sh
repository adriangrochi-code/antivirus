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
echo "sched_ext=$([ -d /sys/kernel/sched_ext ] && echo si || echo no)"
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
check "sched_ext=si" "sched_ext ausente"
[[ $fail == 0 ]] && echo "OK: el kernel arranca con todas las protecciones activas"
exit $fail
