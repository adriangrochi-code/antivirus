// SPDX-License-Identifier: GPL-2.0
/*
 * tisma_uv: undervolt de CPU para AMD Excavator (familia 15h, modelos 60h-6Fh: Carrizo y Bristol Ridge).
 *
 * El lockdown del kernel (que TISMA mantiene siempre) prohíbe escribir los registros MSR desde programas: también
 * sirven para atacar el sistema. Este módulo, firmado con la clave de TISMA, hace UNA sola cosa y acotada:
 *   - solo BAJA el voltaje de los P-states (CpuVid más alto = menos voltaje, SVI2: V = 1,55 V - VID x 6,25 mV);
 *   - como mucho 24 pasos (150 mV); nunca sube el voltaje ni toca frecuencias, divisores ni otros bits;
 *   - guarda los valores de fábrica al cargar y los repone al descargarse, al apagar y con «pasos = 0»;
 *   - reaplica el ajuste al volver de la suspensión (el firmware puede reponer los de fábrica).
 * Interfaz: /sys/kernel/tisma_uv/pasos (0-24, escribir solo root) y /sys/kernel/tisma_uv/estado.
 * La búsqueda del mínimo estable la hace centinela (autotune) con pruebas de estabilidad y vuelta a fábrica
 * si el equipo se cuelga.
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/kobject.h>
#include <linux/sysfs.h>
#include <linux/cpu.h>
#include <linux/mutex.h>
#include <linux/reboot.h>
#include <linux/suspend.h>
#include <linux/version.h>
#include <asm/msr.h>
#include <asm/processor.h>

#define MSR_PSTATE_CTL    0xc0010062
#define MSR_PSTATE_STATUS 0xc0010063
#define MSR_PSTATE_DEF(n) (0xc0010064 + (n))
#define NUM_PSTATES       8
#define VID_SHIFT         9
#define VID_MASK          (0x7fULL << VID_SHIFT)
#define PSTATE_EN         (1ULL << 63)
#define MAX_PASOS         24          /* 24 x 6,25 mV = 150 mV */
#define MAX_VID           0x7c        /* nunca por debajo de ~0,775 V */

#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 16, 0)
#define LEER(cpu, msr, v)     rdmsrq_on_cpu(cpu, msr, v)
#define ESCRIBIR(cpu, msr, v) wrmsrq_on_cpu(cpu, msr, v)
#else
#define LEER(cpu, msr, v)     rdmsrl_on_cpu(cpu, msr, v)
#define ESCRIBIR(cpu, msr, v) wrmsrl_on_cpu(cpu, msr, v)
#endif

static u64 fabrica[NUM_PSTATES];
static int pasos;
static DEFINE_MUTEX(lock);
static struct kobject *kobj;

static int aplicar(int n)
{
	unsigned int cpu;
	int p;
	u64 v, st;

	cpus_read_lock();
	for_each_online_cpu(cpu) {
		for (p = 0; p < NUM_PSTATES; p++) {
			u64 vid;

			if (!(fabrica[p] & PSTATE_EN))
				continue;
			vid = ((fabrica[p] & VID_MASK) >> VID_SHIFT) + n;
			if (vid > MAX_VID) {
				cpus_read_unlock();
				return -ERANGE;
			}
			v = (fabrica[p] & ~VID_MASK) | (vid << VID_SHIFT);
			if (ESCRIBIR(cpu, MSR_PSTATE_DEF(p), v))
				goto mal;
		}
		/* Volver a pedir el P-state actual: el núcleo toma la definición nueva ya */
		if (LEER(cpu, MSR_PSTATE_STATUS, &st) || ESCRIBIR(cpu, MSR_PSTATE_CTL, st & 0x7))
			goto mal;
	}
	cpus_read_unlock();
	pasos = n;
	return 0;
mal:
	cpus_read_unlock();
	return -EIO;
}

static ssize_t pasos_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
	return sysfs_emit(buf, "%d\n", pasos);
}

static ssize_t pasos_store(struct kobject *k, struct kobj_attribute *a, const char *buf, size_t count)
{
	int n, r;

	if (kstrtoint(buf, 10, &n) || n < 0 || n > MAX_PASOS)
		return -EINVAL;
	mutex_lock(&lock);
	r = aplicar(n);
	if (r)
		aplicar(0);                       /* ante cualquier error, de vuelta a fábrica */
	mutex_unlock(&lock);
	return r ? r : count;
}

static ssize_t estado_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
	int p, len = 0;

	len += sysfs_emit_at(buf, len, "pasos %d (-%d,%02d mV)\n", pasos, pasos * 625 / 100, pasos * 625 % 100);
	for (p = 0; p < NUM_PSTATES; p++)
		if (fabrica[p] & PSTATE_EN)
			len += sysfs_emit_at(buf, len, "P%d vid_fabrica 0x%02llx\n", p,
					     (fabrica[p] & VID_MASK) >> VID_SHIFT);
	return len;
}

static struct kobj_attribute attr_pasos = __ATTR(pasos, 0644, pasos_show, pasos_store);
static struct kobj_attribute attr_estado = __ATTR_RO(estado);
static struct attribute *attrs[] = { &attr_pasos.attr, &attr_estado.attr, NULL };
static const struct attribute_group grupo = { .attrs = attrs };

static int al_apagar(struct notifier_block *nb, unsigned long code, void *x)
{
	mutex_lock(&lock);
	aplicar(0);
	mutex_unlock(&lock);
	return NOTIFY_DONE;
}

static int al_dormir(struct notifier_block *nb, unsigned long ev, void *x)
{
	if (ev == PM_POST_SUSPEND || ev == PM_POST_HIBERNATION) {
		mutex_lock(&lock);
		if (pasos && aplicar(pasos))
			aplicar(0);
		mutex_unlock(&lock);
	}
	return NOTIFY_DONE;
}

static struct notifier_block nb_apagar = { .notifier_call = al_apagar };
static struct notifier_block nb_dormir = { .notifier_call = al_dormir };

static int __init tisma_uv_init(void)
{
	int p, r;

	if (boot_cpu_data.x86_vendor != X86_VENDOR_AMD || boot_cpu_data.x86 != 0x15 ||
	    boot_cpu_data.x86_model < 0x60 || boot_cpu_data.x86_model > 0x6f) {
		pr_info("tisma_uv: procesador no compatible (solo AMD familia 15h, modelos 60h-6Fh)\n");
		return -ENODEV;
	}
	for (p = 0; p < NUM_PSTATES; p++) {
		if (LEER(0, MSR_PSTATE_DEF(p), &fabrica[p]))
			return -EIO;
		if ((fabrica[p] & PSTATE_EN) && (fabrica[p] & (1ULL << 21))) {
			/* CpuVid de 8 bits en uso: formato que este módulo no maneja, mejor no tocar nada */
			pr_info("tisma_uv: formato de VID no soportado\n");
			return -ENODEV;
		}
	}
	kobj = kobject_create_and_add("tisma_uv", kernel_kobj);
	if (!kobj)
		return -ENOMEM;
	r = sysfs_create_group(kobj, &grupo);
	if (r) {
		kobject_put(kobj);
		return r;
	}
	register_reboot_notifier(&nb_apagar);
	register_pm_notifier(&nb_dormir);
	pr_info("tisma_uv: listo (pasos 0 = fábrica)\n");
	return 0;
}

static void __exit tisma_uv_exit(void)
{
	unregister_pm_notifier(&nb_dormir);
	unregister_reboot_notifier(&nb_apagar);
	mutex_lock(&lock);
	aplicar(0);
	mutex_unlock(&lock);
	sysfs_remove_group(kobj, &grupo);
	kobject_put(kobj);
}

module_init(tisma_uv_init);
module_exit(tisma_uv_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("TISMA: undervolt acotado (solo bajar) para AMD Excavator (familia 15h, 60h-6Fh)");
