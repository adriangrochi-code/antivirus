// Prueba de páginas grandes (THP) y medianas (mTHP) en la VM: reserva 64 MB de memoria normal (sin madvise),
// la toca entera y cuenta cuántas páginas de 2 MB y de 64 kB le dio el kernel.
// Imprime «paginas_2m_kb=N mthp64=M». Compilar estático: gcc -O2 -static -o paginas-test paginas-test.c
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>

static long leer_num(const char *ruta, const char *clave) {
    FILE *f = fopen(ruta, "r");
    char linea[256];
    long v = -1;
    if (!f) return -1;
    while (fgets(linea, sizeof linea, f)) {
        if (!clave) { sscanf(linea, "%ld", &v); break; }
        if (!strncmp(linea, clave, strlen(clave))) { sscanf(linea + strlen(clave), " %ld", &v); break; }
    }
    fclose(f);
    return v;
}

int main(void) {
    const size_t tam = 64 << 20;
    const char *mthp = "/sys/kernel/mm/transparent_hugepage/hugepages-64kB/stats/anon_fault_alloc";
    long antes = leer_num(mthp, NULL);
    // alineado a 2 MB para que la página grande sea posible
    char *p = mmap(NULL, tam + (2 << 20), PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) return 1;
    char *a = (char *)(((unsigned long)p + (2 << 20) - 1) & ~((2UL << 20) - 1));
    for (size_t i = 0; i < tam; i += 4096) a[i] = 1;
    printf("paginas_2m_kb=%ld mthp64=%ld\n", leer_num("/proc/meminfo", "AnonHugePages:"),
           leer_num(mthp, NULL) - antes);
    return 0;
}
