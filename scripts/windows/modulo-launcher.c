/*
 * modulo-launcher.c — Windows native launcher for the Modulo single-install bundle.
 *
 * This launcher is a minimal native executable that:
 * 1. Locates its own executable directory
 * 2. Constructs the bundle root (parent of the launcher's directory)
 * 3. Sets PYTHONPATH to bundle_root/backend/src and site-packages
 * 4. Sets PATH to include bundle_root/runtime/bin, bundle_root/pg/bin, bundle_root/redis
 * 5. Sets MODULO_BUNDLED_BIN_DIR to bundle_root/bin
 * 6. Execs runtime/bin/python3.exe -m modulo.cli.main with original arguments
 *
 * Build with MSVC (Visual Studio Developer Command Prompt):
 *   cl /nologo /O2 /Wall /WX /Fe:modulo.exe scripts/windows/modulo-launcher.c
 *
 * Or with clang-cl (if available):
 *   clang-cl /nologo /O2 /Wall /WX /Fe:modulo.exe scripts/windows/modulo-launcher.c
 *
 * The resulting modulo.exe should be placed in the bundle's bin/ directory.
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_PATH_LEN 32767

/* Helper: get the directory containing this executable */
static BOOL get_executable_dir(wchar_t *buf, DWORD buf_len) {
    DWORD len = GetModuleFileNameW(NULL, buf, buf_len);
    if (len == 0 || len >= buf_len) {
        return FALSE;
    }
    /* Strip the executable name, keep only the directory */
    wchar_t *last_slash = wcsrchr(buf, L'\\');
    if (last_slash) {
        *last_slash = L'\0';
    }
    return TRUE;
}

/* Helper: append a path component, handling separators */
static void path_append(wchar_t *dest, size_t dest_len, const wchar_t *component) {
    size_t dest_cur = wcslen(dest);
    if (dest_cur > 0 && dest[dest_cur - 1] != L'\\') {
        if (dest_cur + 1 < dest_len) {
            dest[dest_cur] = L'\\';
            dest[dest_cur + 1] = L'\0';
        }
    }
    wcsncat(dest, component, dest_len - wcslen(dest) - 1);
}

/* Helper: set environment variable (wide) */
static void set_env(const wchar_t *name, const wchar_t *value) {
    _wputenv_s(name, value);
}

/* Helper: prepend to PATH (wide) */
static void prepend_path(const wchar_t *new_path) {
    wchar_t *old_path = _wgetenv(L"PATH");
    size_t new_len = wcslen(new_path);
    size_t old_len = old_path ? wcslen(old_path) : 0;
    wchar_t *combined = (wchar_t *)malloc((new_len + 1 + old_len + 1) * sizeof(wchar_t));
    if (combined) {
        wcscpy(combined, new_path);
        if (old_len > 0) {
            combined[new_len] = L';';
            wcscpy(combined + new_len + 1, old_path);
        }
        _wputenv_s(L"PATH", combined);
        free(combined);
    }
}

/* Helper: build PYTHONPATH from multiple components */
static void build_pythonpath(const wchar_t *bundle_root) {
    wchar_t pythonpath[MAX_PATH_LEN * 4] = {0};
    wchar_t component[MAX_PATH_LEN];

    /* bundle_root/backend/src */
    wcscpy(component, bundle_root);
    path_append(component, MAX_PATH_LEN, L"backend");
    path_append(component, MAX_PATH_LEN, L"src");
    wcscpy(pythonpath, component);

    /* bundle_root/runtime/Lib/site-packages */
    wcscpy(component, bundle_root);
    path_append(component, MAX_PATH_LEN, L"runtime");
    path_append(component, MAX_PATH_LEN, L"Lib");
    path_append(component, MAX_PATH_LEN, L"site-packages");
    wcscat(pythonpath, L";");
    wcscat(pythonpath, component);

    /* bundle_root/runtime/Lib */
    wcscpy(component, bundle_root);
    path_append(component, MAX_PATH_LEN, L"runtime");
    path_append(component, MAX_PATH_LEN, L"Lib");
    wcscat(pythonpath, L";");
    wcscat(pythonpath, component);

    _wputenv_s(L"PYTHONPATH", pythonpath);
}

/* Convert command line arguments to a single command string for CreateProcess */
static wchar_t *build_command_line(const wchar_t *python_exe, int argc, wchar_t *argv[]) {
    /* Calculate total length needed */
    size_t total_len = wcslen(python_exe) + 1; /* +1 for space */
    for (int i = 1; i < argc; i++) { /* Skip argv[0] (our exe name) */
        total_len += wcslen(argv[i]) + 3; /* +3 for space, quotes, null */
    }

    wchar_t *cmdline = (wchar_t *)malloc((total_len + 1) * sizeof(wchar_t));
    if (!cmdline) return NULL;

    wcscpy(cmdline, python_exe);
    for (int i = 1; i < argc; i++) {
        wcscat(cmdline, L" ");
        /* Quote arguments that contain spaces */
        if (wcschr(argv[i], L' ') || wcschr(argv[i], L'\t')) {
            wcscat(cmdline, L"\"");
            wcscat(cmdline, argv[i]);
            wcscat(cmdline, L"\"");
        } else {
            wcscat(cmdline, argv[i]);
        }
    }
    return cmdline;
}

int wmain(int argc, wchar_t *argv[]) {
    wchar_t exe_dir[MAX_PATH_LEN];
    wchar_t bundle_root[MAX_PATH_LEN];
    wchar_t python_exe[MAX_PATH_LEN];
    wchar_t path_component[MAX_PATH_LEN];

    /* 1. Locate our executable directory */
    if (!get_executable_dir(exe_dir, MAX_PATH_LEN)) {
        fwprintf(stderr, L"Failed to get executable directory\n");
        return 1;
    }

    /* 2. Construct bundle root (parent of launcher's directory) */
    /* Launcher lives in bundle_root/bin/, so bundle_root = parent of exe_dir */
    wcscpy(bundle_root, exe_dir);
    wchar_t *last_slash = wcsrchr(bundle_root, L'\\');
    if (last_slash) {
        *last_slash = L'\0';
    } else {
        fwprintf(stderr, L"Unexpected launcher location: %s\n", exe_dir);
        return 1;
    }

    /* 3. Set MODULO_BUNDLED_BIN_DIR to bundle_root/bin */
    wcscpy(path_component, bundle_root);
    path_append(path_component, MAX_PATH_LEN, L"bin");
    set_env(L"MODULO_BUNDLED_BIN_DIR", path_component);

    /* 4. Set PYTHONPATH */
    build_pythonpath(bundle_root);

    /* 5. Prepend PATH entries (order matters - first wins) */
    /* bundle_root/runtime/bin - Python DLLs and scripts */
    wcscpy(path_component, bundle_root);
    path_append(path_component, MAX_PATH_LEN, L"runtime");
    path_append(path_component, MAX_PATH_LEN, L"bin");
    prepend_path(path_component);

    /* bundle_root/pg/bin - PostgreSQL binaries */
    wcscpy(path_component, bundle_root);
    path_append(path_component, MAX_PATH_LEN, L"pg");
    path_append(path_component, MAX_PATH_LEN, L"bin");
    prepend_path(path_component);

    /* bundle_root/redis - Valkey/Redis binaries */
    wcscpy(path_component, bundle_root);
    path_append(path_component, MAX_PATH_LEN, L"redis");
    prepend_path(path_component);

    /* 6. Locate python executable */
    wcscpy(python_exe, bundle_root);
    path_append(python_exe, MAX_PATH_LEN, L"runtime");
    path_append(python_exe, MAX_PATH_LEN, L"bin");
    path_append(python_exe, MAX_PATH_LEN, L"python3.exe");

    /* Verify python.exe exists */
    if (GetFileAttributesW(python_exe) == INVALID_FILE_ATTRIBUTES) {
        fwprintf(stderr, L"Python executable not found at: %s\n", python_exe);
        fwprintf(stderr, L"Bundle root: %s\n", bundle_root);
        return 1;
    }

    /* 7. Build command line: python3.exe -m modulo.cli.main [original args] */
    wchar_t *cmdline = build_command_line(python_exe, argc, argv);
    if (!cmdline) {
        fwprintf(stderr, L"Failed to build command line\n");
        return 1;
    }

    /* 8. Execute python with the module entry point */
    STARTUPINFOW si = {0};
    PROCESS_INFORMATION pi = {0};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE; /* Hide console window */

    BOOL success = CreateProcessW(
        NULL,           /* Application name (use command line) */
        cmdline,        /* Command line */
        NULL,           /* Process security attributes */
        NULL,           /* Thread security attributes */
        TRUE,           /* Inherit handles */
        0,              /* Creation flags */
        NULL,           /* Environment (use parent's modified env) */
        bundle_root,    /* Current directory (bundle root) */
        &si,            /* Startup info */
        &pi             /* Process information */
    );

    if (!success) {
        DWORD err = GetLastError();
        fwprintf(stderr, L"Failed to launch Python (error %lu): %s\\n", err, cmdline);
        free(cmdline);
        return 1;
    }

    free(cmdline);

    /* Wait for the child process to complete */
    WaitForSingleObject(pi.hProcess, INFINITE);

    DWORD exit_code = 0;
    GetExitCodeProcess(pi.hProcess, &exit_code);

    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);

    return (int)exit_code;
}
