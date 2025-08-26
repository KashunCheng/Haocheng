#include <stdio.h>
#include <stdlib.h>

static int read_n(void) {
    char *path = getenv("CODEX_STDIN_FILE");
    int n = 0;
    if (path) {
        FILE *f = fopen(path, "rb");
        if (!f) return 0;
        if (fscanf(f, "%d", &n) != 1) n = 0;
        fclose(f);
    } else {
        if (scanf("%d", &n) != 1) n = 0;  // true stdin path
    }
    return n;
}

int work_stdin(int n) {
    int acc = 1;
    for (int i = 1; i <= n; i++) {
        acc *= i;                   // BREAK HERE (loop body)
        // (watch i, acc)
    }
    return acc;
}

int main(void) {
    int n = read_n();
    int a = work_stdin(n);
    printf("acc=%d\n", a);
    return 0;
}

