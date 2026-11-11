// centinela-estres: prueba de estabilidad VERIFICABLE (no solo carga).
//
// Un overclock o undervolt inestable suele producir primero errores de cálculo silenciosos y
// solo después un cuelgue. Por eso cada hilo repite un cálculo de resultado conocido y lo compara
// bit a bit con la referencia calculada al inicio (a frecuencia/voltaje todavía seguros):
//   cpu  multiplicación de matrices en coma flotante (FMA) + mezcla entera tipo hash
//   mem  escribe patrones en un bloque grande y los verifica (detecta errores de RAM/controlador)
// Salida: JSON {"ok":..., "errores":..., "iteraciones":..., "segundos":...}
// Uso: centinela-estres [cpu|mem|todo] <segundos> [hilos] [MB]
// Compilar: cc -O2 -pthread -o centinela-estres estres.c
#define _GNU_SOURCE
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define N 96
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }

static _Atomic long errores, iteraciones, sin_memoria;
static double fin;
static double ref_mat;
static uint64_t ref_hash;

static double matmul(unsigned seed) {
    static __thread double a[N][N], b[N][N], c[N][N];
    for (int i = 0; i < N; i++)
        for (int j = 0; j < N; j++) {
            a[i][j] = ((i * 131 + j * 7 + seed) % 97) / 97.0;
            b[i][j] = ((i * 17 + j * 59 + seed) % 89) / 89.0;
        }
    for (int i = 0; i < N; i++)
        for (int j = 0; j < N; j++) {
            double s = 0;
            for (int k = 0; k < N; k++) s += a[i][k] * b[k][j];
            c[i][j] = s;
        }
    double sum = 0;
    for (int i = 0; i < N; i++) sum += c[i][(i * 13) % N];
    return sum;
}

static uint64_t mezcla(uint64_t x) {
    for (int i = 0; i < 200000; i++) {
        x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL; x ^= x >> 33;
        x += (uint64_t)i * 0x9e3779b97f4a7c15ULL;
    }
    return x;
}

static void *hilo_cpu(void *arg) {
    (void)arg;
    while (now() < fin) {
        double m = matmul(12345);
        uint64_t h = mezcla(42);
        if (memcmp(&m, &ref_mat, sizeof m) != 0 || h != ref_hash) atomic_fetch_add(&errores, 1);
        atomic_fetch_add(&iteraciones, 1);
    }
    return NULL;
}

static size_t mem_bytes;
static void *hilo_mem(void *arg) {
    size_t n = mem_bytes / sizeof(uint64_t);
    uint64_t *buf = malloc(mem_bytes);
    if (!buf) {                       // sin memoria para la prueba: no se puede dar por superada
        atomic_fetch_add(&sin_memoria, 1);
        return NULL;
    }
    uint64_t semilla = (uint64_t)(uintptr_t)arg * 0x9e3779b97f4a7c15ULL;
    while (now() < fin) {
        for (size_t i = 0; i < n; i++) buf[i] = (i * 0x9e3779b97f4a7c15ULL) ^ semilla;
        for (size_t i = 0; i < n; i++)
            if (buf[i] != ((i * 0x9e3779b97f4a7c15ULL) ^ semilla)) atomic_fetch_add(&errores, 1);
        for (size_t i = 0; i < n; i++) buf[i] = ~buf[i];           // bits invertidos
        for (size_t i = 0; i < n; i++)
            if (buf[i] != ~((i * 0x9e3779b97f4a7c15ULL) ^ semilla)) atomic_fetch_add(&errores, 1);
        semilla += 1;
        atomic_fetch_add(&iteraciones, 1);
    }
    free(buf);
    return NULL;
}

int main(int argc, char **argv) {
    const char *modo = argc > 1 ? argv[1] : "todo";
    double segundos = argc > 2 ? atof(argv[2]) : 30;
    int hilos = argc > 3 ? atoi(argv[3]) : (int)sysconf(_SC_NPROCESSORS_ONLN);
    long mb = argc > 4 ? atol(argv[4]) : 256;
    if (hilos < 1) hilos = 1;
    if (hilos > 1024) hilos = 1024;
    if (segundos < 1 || segundos > 3600 || mb < 16 || mb > 16384) {
        fprintf(stderr, "argumentos fuera de rango\n");
        return 2;
    }
    ref_mat = matmul(12345);          // referencias calculadas antes de cargar la máquina
    ref_hash = mezcla(42);
    int cpu = strcmp(modo, "mem") != 0, mem = strcmp(modo, "cpu") != 0;
    int total = (cpu ? hilos : 0) + (mem ? (hilos + 1) / 2 : 0);
    pthread_t *th = calloc((size_t)total, sizeof *th);
    if (!th) {
        fprintf(stderr, "sin memoria\n");
        return 2;
    }
    mem_bytes = (size_t)mb * 1024 * 1024 / (size_t)((hilos + 1) / 2);
    double t0 = now();
    fin = t0 + segundos;
    int k = 0;
    for (int i = 0; cpu && i < hilos; i++) pthread_create(&th[k++], NULL, hilo_cpu, NULL);
    for (int i = 0; mem && i < (hilos + 1) / 2; i++) pthread_create(&th[k++], NULL, hilo_mem, (void *)(uintptr_t)(i + 1));
    for (int i = 0; i < k; i++) pthread_join(th[i], NULL);
    int ok = !errores && !sin_memoria;
    printf("{\"ok\": %s, \"errores\": %ld, \"sin_memoria\": %ld, \"iteraciones\": %ld, \"segundos\": %.1f, "
           "\"modo\": \"%s\"}\n", ok ? "true" : "false", (long)errores, (long)sin_memoria, (long)iteraciones,
           now() - t0, modo);
    return ok ? 0 : 1;
}
