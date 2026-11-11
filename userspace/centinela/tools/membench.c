// centinela-membench: mide el subsistema de memoria y la topología de la CPU.
//
//   bandwidth  lectura / escritura / copia en GB/s (multihilo, un hilo por CPU)
//   latency    latencia de acceso aleatorio (pointer chasing) para varios tamaños,
//              lo que revela L1/L2/L3/RAM
//   c2c        latencia núcleo a núcleo (ping-pong de una línea de caché):
//              revela CCDs, clusters de núcleos E y sockets
//   --sched    latencia del planificador (sin fijar CPUs: la decide el kernel): ida y vuelta entre dos hilos
//              por tuberías (despertar un proceso) y retraso de un temporizador de 1 ms (lo que nota el
//              escritorio y el audio). Lo usa «centinela comparar» para medir HZ, preempción y sched_ext.
//
// Salida: JSON por stdout. Compilar: cc -O2 -pthread -o centinela-membench membench.c
#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static double now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static void pin(int cpu) {
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
}

/* ------------------------------------------------------------ ancho de banda */
enum { OP_READ, OP_WRITE, OP_COPY };
typedef struct {
    int cpu, op, iters;
    size_t bytes;
    uint64_t *a, *b;
    pthread_barrier_t *bar;
    double secs;
    uint64_t sink;
} bw_arg;

static void *bw_worker(void *p) {
    bw_arg *w = p;
    pin(w->cpu);
    size_t n = w->bytes / sizeof(uint64_t);
    w->a = aligned_alloc(64, w->bytes);
    w->b = aligned_alloc(64, w->bytes);
    if (!w->a || !w->b) { fprintf(stderr, "membench: sin memoria\n"); exit(2); }
    memset(w->a, 1, w->bytes);
    memset(w->b, 2, w->bytes);
    pthread_barrier_wait(w->bar);
    double t0 = now();
    uint64_t acc = 0;
    for (int it = 0; it < w->iters; it++) {
        if (w->op == OP_READ) {
            uint64_t s0 = 0, s1 = 0, s2 = 0, s3 = 0;
            for (size_t i = 0; i < n; i += 4) {
                s0 += w->a[i]; s1 += w->a[i + 1]; s2 += w->a[i + 2]; s3 += w->a[i + 3];
            }
            acc += s0 + s1 + s2 + s3;
        } else if (w->op == OP_WRITE) {
            memset(w->a, it & 0xff, w->bytes);
        } else {
            memcpy(w->b, w->a, w->bytes);
        }
    }
    w->secs = now() - t0;
    w->sink = acc + w->b[n - 1];
    free(w->a);
    free(w->b);
    return NULL;
}

static double bandwidth(int op, int nthreads, size_t bytes_per_thread, int iters) {
    pthread_t th[nthreads];
    bw_arg args[nthreads];
    pthread_barrier_t bar;
    pthread_barrier_init(&bar, NULL, nthreads);
    for (int i = 0; i < nthreads; i++) {
        args[i] = (bw_arg){.cpu = i, .op = op, .iters = iters, .bytes = bytes_per_thread, .bar = &bar};
        pthread_create(&th[i], NULL, bw_worker, &args[i]);
    }
    double worst = 0;
    for (int i = 0; i < nthreads; i++) {
        pthread_join(th[i], NULL);
        if (args[i].secs > worst) worst = args[i].secs;
    }
    pthread_barrier_destroy(&bar);
    double moved = (double)bytes_per_thread * iters * nthreads * (op == OP_COPY ? 2 : 1);
    return moved / worst / 1e9;
}

/* ------------------------------------------------------------ latencia */
static double latency_ns(size_t bytes) {
    size_t n = bytes / sizeof(void *);
    void **buf = aligned_alloc(64, n * sizeof(void *));
    size_t *perm = malloc(n * sizeof(size_t));
    if (!buf || !perm) { fprintf(stderr, "membench: sin memoria\n"); exit(2); }
    for (size_t i = 0; i < n; i++) perm[i] = i;
    srand(1234);
    for (size_t i = n - 1; i > 0; i--) {  // Fisher-Yates: recorrido aleatorio
        size_t j = ((size_t)rand() * RAND_MAX + rand()) % (i + 1);
        size_t t = perm[i]; perm[i] = perm[j]; perm[j] = t;
    }
    for (size_t i = 0; i < n; i++) buf[perm[i]] = &buf[perm[(i + 1) % n]];
    free(perm);
    void **p = &buf[0];
    size_t steps = 20 * 1000 * 1000;
    for (size_t i = 0; i < n; i++) p = *p;  // calentar
    double t0 = now();
    for (size_t i = 0; i < steps; i++) p = *p;
    double t = now() - t0;
    if (p == NULL) puts("");  // evita que el compilador elimine el bucle
    free(buf);
    return t / steps * 1e9;
}

/* ------------------------------------------------------------ núcleo a núcleo */
static _Atomic int flag __attribute__((aligned(64)));
typedef struct { int cpu, rounds; } c2c_arg;

static void *c2c_pong(void *p) {
    c2c_arg *a = p;
    pin(a->cpu);
    for (int i = 0; i < a->rounds; i++) {
        while (atomic_load_explicit(&flag, memory_order_acquire) != 1) ;
        atomic_store_explicit(&flag, 0, memory_order_release);
    }
    return NULL;
}

static double c2c_ns(int a, int b) {
    int rounds = 20000;
    c2c_arg arg = {b, rounds};
    atomic_store(&flag, 0);
    pthread_t th;
    pthread_create(&th, NULL, c2c_pong, &arg);
    pin(a);
    double t0 = now();
    for (int i = 0; i < rounds; i++) {
        atomic_store_explicit(&flag, 1, memory_order_release);
        while (atomic_load_explicit(&flag, memory_order_acquire) != 0) ;
    }
    double t = now() - t0;
    pthread_join(th, NULL);
    return t / rounds / 2 * 1e9;  // ida
}

/* ------------------------------------------------------------ planificador */
static int pp_a[2], pp_b[2];
static void *sched_pong(void *p) {
    int n = *(int *)p;
    char c;
    for (int i = 0; i < n; i++) {
        if (read(pp_a[0], &c, 1) != 1 || write(pp_b[1], &c, 1) != 1) break;
    }
    return NULL;
}

static int cmp_d(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

static int sched_bench(void) {
    enum { RT = 20000, TIMERS = 400 };
    if (pipe(pp_a) || pipe(pp_b)) return 1;
    int n = RT;
    pthread_t th;
    pthread_create(&th, NULL, sched_pong, &n);
    char c = 'x';
    double t0 = now();
    for (int i = 0; i < RT; i++) {
        if (write(pp_a[1], &c, 1) != 1 || read(pp_b[0], &c, 1) != 1) return 1;
    }
    double pingpong_us = (now() - t0) / RT * 1e6;
    pthread_join(th, NULL);

    static double over[TIMERS];
    struct timespec req = {0, 1000000};
    for (int i = 0; i < TIMERS; i++) {
        double a = now();
        nanosleep(&req, NULL);
        over[i] = (now() - a) * 1e6 - 1000.0;
    }
    qsort(over, TIMERS, sizeof over[0], cmp_d);
    printf("{\"pingpong_us\": %.2f, \"timer_p50_us\": %.1f, \"timer_p99_us\": %.1f}\n",
           pingpong_us, over[TIMERS / 2], over[TIMERS * 99 / 100]);
    return 0;
}

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "--sched") == 0) return sched_bench();
    int ncpu = (int)sysconf(_SC_NPROCESSORS_ONLN);
    int quick = argc > 1 && strcmp(argv[1], "--quick") == 0;
    size_t per_thread = (quick ? 32 : 128) << 20;
    int iters = quick ? 2 : 5;

    printf("{\n  \"cpus\": %d,\n", ncpu);
    printf("  \"bandwidth_gbs\": {\"read\": %.2f, \"write\": %.2f, \"copy\": %.2f, \"read_single_thread\": %.2f},\n",
           bandwidth(OP_READ, ncpu, per_thread, iters), bandwidth(OP_WRITE, ncpu, per_thread, iters),
           bandwidth(OP_COPY, ncpu, per_thread, iters), bandwidth(OP_READ, 1, per_thread, iters));

    size_t sizes_kb[] = {16, 256, 2048, 16384, 262144};
    printf("  \"latency_ns\": {");
    for (int i = 0; i < 5; i++)
        printf("%s\"%zu\": %.2f", i ? ", " : "", sizes_kb[i], latency_ns(sizes_kb[i] << 10));
    printf("},\n");

    int maxc = quick ? (ncpu < 8 ? ncpu : 8) : (ncpu < 64 ? ncpu : 64);
    printf("  \"core_to_core_ns\": [");
    for (int a = 0; a < maxc; a++) {
        printf("%s[", a ? ", " : "");
        for (int b = 0; b < maxc; b++)
            printf("%s%.1f", b ? ", " : "", a == b ? 0.0 : c2c_ns(a, b));
        printf("]");
    }
    printf("]\n}\n");
    return 0;
}
