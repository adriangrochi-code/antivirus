// Prueba del driver binder (el de Waydroid) dentro de la VM de prueba: monta binderfs, crea un dispositivo
// con binder-control como hace Waydroid, lo abre, comprueba la versión del protocolo, reserva su búfer y hace
// de gestor de contexto (servicemanager) y hace una transacción de ida y vuelta entre dos procesos. Imprime «binder_prueba=ok» o el paso que falló.
// Compilar estático: gcc -O2 -static -o binder-test binder-test.c
#include <errno.h>
#include <fcntl.h>
#include <linux/android/binder.h>
#include <linux/android/binderfs.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static int fallo(const char *paso) {
    printf("binder_prueba=FALLO en %s: %s\n", paso, strerror(errno));
    return 1;
}


// Escribe un comando y lee la respuesta del driver hasta encontrar «esperado»; copia su transacción en «tr».
static int ciclo(int fd, const void *escr, size_t n, __u32 esperado, struct binder_transaction_data *tr) {
    unsigned char leido[512];
    struct binder_write_read bwr = {.write_size = n, .write_buffer = (binder_uintptr_t)escr};
    for (int vuelta = 0; vuelta < 20; vuelta++) {
        bwr.read_size = sizeof leido;
        bwr.read_consumed = 0;
        bwr.read_buffer = (binder_uintptr_t)leido;
        if (ioctl(fd, BINDER_WRITE_READ, &bwr)) return -1;
        bwr.write_size = 0;
        for (size_t i = 0; i + 4 <= bwr.read_consumed;) {
            __u32 cmd;
            memcpy(&cmd, leido + i, 4);
            i += 4;
            if (cmd == esperado) {
                if (tr) memcpy(tr, leido + i, sizeof *tr);
                return 0;
            }
            i += _IOC_SIZE(cmd);
        }
    }
    errno = ETIMEDOUT;
    return -1;
}

int main(void) {
    mkdir("/dev/binderfs", 0755);
    if (mount("binder", "/dev/binderfs", "binder", 0, NULL) && errno != EBUSY) return fallo("mount binderfs");
    int ctl = open("/dev/binderfs/binder-control", O_RDWR | O_CLOEXEC);
    if (ctl < 0) return fallo("open binder-control");
    struct binderfs_device dev = {0};
    strcpy(dev.name, "anbox-binder");
    if (ioctl(ctl, BINDER_CTL_ADD, &dev)) return fallo("BINDER_CTL_ADD");
    int fd = open("/dev/binderfs/anbox-binder", O_RDWR | O_CLOEXEC);
    if (fd < 0) return fallo("open anbox-binder");
    struct binder_version v = {0};
    if (ioctl(fd, BINDER_VERSION, &v)) return fallo("BINDER_VERSION");
    if (v.protocol_version != BINDER_CURRENT_PROTOCOL_VERSION) {
        printf("binder_prueba=FALLO versión %d\n", v.protocol_version);
        return 1;
    }
    if (mmap(NULL, 1 << 20, PROT_READ, MAP_PRIVATE, fd, 0) == MAP_FAILED) return fallo("mmap");
    __u32 hilos = 4;
    if (ioctl(fd, BINDER_SET_MAX_THREADS, &hilos)) return fallo("BINDER_SET_MAX_THREADS");
    if (ioctl(fd, BINDER_SET_CONTEXT_MGR, 0)) return fallo("BINDER_SET_CONTEXT_MGR");
    // Cliente: otro proceso abre el dispositivo y llama al gestor de contexto (handle 0)
    pid_t hijo = fork();
    if (hijo == 0) {
        int c = open("/dev/binderfs/anbox-binder", O_RDWR | O_CLOEXEC);
        if (c < 0 || mmap(NULL, 1 << 20, PROT_READ, MAP_PRIVATE, c, 0) == MAP_FAILED) _exit(2);
        const char msg[] = "hola";
        struct { __u32 cmd; struct binder_transaction_data tr; } __attribute__((packed)) tx = {.cmd = BC_TRANSACTION};
        tx.tr.target.handle = 0;
        tx.tr.code = 1;
        tx.tr.data_size = sizeof msg;
        tx.tr.data.ptr.buffer = (binder_uintptr_t)msg;
        struct binder_transaction_data resp;
        if (ciclo(c, &tx, sizeof tx, BR_REPLY, &resp)) _exit(3);
        _exit(resp.data_size == 6 && !memcmp((void *)resp.data.ptr.buffer, "adios", 6) ? 0 : 4);
    }
    // Servidor: espera la transacción y responde
    __u32 entrar = BC_ENTER_LOOPER;
    struct binder_transaction_data rx;
    if (ciclo(fd, &entrar, sizeof entrar, BR_TRANSACTION, &rx)) return fallo("recibir transacción");
    if (rx.code != 1 || rx.data_size != 5 || memcmp((void *)rx.data.ptr.buffer, "hola", 5)) {
        printf("binder_prueba=FALLO datos recibidos\n");
        return 1;
    }
    const char adios[] = "adios";
    struct {
        __u32 libera; binder_uintptr_t buf;
        __u32 cmd; struct binder_transaction_data tr;
    } __attribute__((packed)) rp = {.libera = BC_FREE_BUFFER, .buf = rx.data.ptr.buffer, .cmd = BC_REPLY};
    rp.tr.data_size = sizeof adios;
    rp.tr.data.ptr.buffer = (binder_uintptr_t)adios;
    if (ciclo(fd, &rp, sizeof rp, BR_TRANSACTION_COMPLETE, NULL)) return fallo("responder");
    int estado;
    waitpid(hijo, &estado, 0);
    if (!WIFEXITED(estado) || WEXITSTATUS(estado)) {
        printf("binder_prueba=FALLO cliente (código %d)\n", WEXITSTATUS(estado));
        return 1;
    }
    printf("binder_prueba=ok (protocolo %d, transacción entre procesos correcta)\n", v.protocol_version);
    return 0;
}
