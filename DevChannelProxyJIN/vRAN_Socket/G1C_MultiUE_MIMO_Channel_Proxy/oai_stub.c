/*
 * oai_stub.c — minimal stub for loading libdfts.so standalone.
 *
 * Provides the handful of OAI symbols (logging, tracing, assertions)
 * that libdfts.so references but that are irrelevant for pure DFT use.
 *
 * Build:
 *   gcc -shared -fPIC -o liboai_stub.so oai_stub.c -lm
 */
#include <stdlib.h>
#include <stdint.h>
#include <stdarg.h>

/* ── T tracer stubs ─────────────────────────────────────────────── */
int T_stdout = 0;
volatile int T_active[256] = {0};

struct T_cache_t { int dummy; };
__thread struct T_cache_t *T_cache;
void *T_freelist_head = NULL;

/* ── Logging stubs ──────────────────────────────────────────────── */
struct { int dummy; } g_log = {0};

void logRecord_mt(const char *file, const char *func, int line,
                  int comp, int level, const char *fmt, ...) {
    (void)file; (void)func; (void)line;
    (void)comp; (void)level; (void)fmt;
}

/* ── AssertFatal support ────────────────────────────────────────── */
struct configmodule_interface_s;
struct configmodule_interface_s *uniqCfg = NULL;

void exit_function(const char *file, const char *function,
                   int line, const char *s, const int assert_flag) {
    (void)file; (void)function; (void)line; (void)s;
    if (assert_flag) abort();
    else exit(0);
}

/* ── Matlab dump stub ───────────────────────────────────────────── */
void write_file_matlab(const char *fname, const char *vname,
                       void *data, int length, int dec, char format) {
    (void)fname; (void)vname; (void)data;
    (void)length; (void)dec; (void)format;
}
