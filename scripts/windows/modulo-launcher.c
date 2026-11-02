/* Windows native launcher for the Modulo single-install bundle. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#define MAX_PATH_LEN 32767

static BOOL get_executable_dir(wchar_t *buf, DWORD buf_len) {
    DWORD len = GetModuleFileNameW(NULL, buf, buf_len);
    wchar_t *last_slash;
    if (len == 0 || len >= buf_len) return FALSE;
    last_slash = wcsrchr(buf, L'\\');
    if (last_slash == NULL) return FALSE;
    *last_slash = L'\0';
    return TRUE;
}

static BOOL path_append(wchar_t *dest, size_t dest_len, const wchar_t *component) {
    size_t used = wcslen(dest);
    size_t component_len = wcslen(component);
    size_t separator = used > 0 && dest[used - 1] != L'\\' ? 1 : 0;
    if (used + separator + component_len + 1 > dest_len) return FALSE;
    if (separator) dest[used++] = L'\\';
    wmemcpy(dest + used, component, component_len + 1);
    return TRUE;
}

static BOOL set_env(const wchar_t *name, const wchar_t *value) {
    return _wputenv_s(name, value) == 0;
}

static BOOL prepend_path(const wchar_t *new_path) {
    const wchar_t *old_path = _wgetenv(L"PATH");
    size_t new_len = wcslen(new_path);
    size_t old_len = old_path == NULL ? 0 : wcslen(old_path);
    wchar_t *combined = (wchar_t *)malloc((new_len + (old_len > 0 ? 1 : 0) + old_len + 1) * sizeof(wchar_t));
    int result;
    if (combined == NULL) return FALSE;
    wcscpy(combined, new_path);
    if (old_len > 0) {
        combined[new_len] = L';';
        wcscpy(combined + new_len + 1, old_path);
    }
    result = _wputenv_s(L"PATH", combined);
    free(combined);
    return result == 0;
}

static BOOL build_pythonpath(const wchar_t *bundle_root) {
    wchar_t pythonpath[MAX_PATH_LEN * 4] = {0};
    wchar_t component[MAX_PATH_LEN];
    if (wcslen(bundle_root) >= MAX_PATH_LEN) return FALSE;
    wcscpy(component, bundle_root);
    if (!path_append(component, MAX_PATH_LEN, L"backend") || !path_append(component, MAX_PATH_LEN, L"src")) return FALSE;
    wcscpy(pythonpath, component);
    wcscpy(component, bundle_root);
    if (!path_append(component, MAX_PATH_LEN, L"runtime") || !path_append(component, MAX_PATH_LEN, L"Lib") || !path_append(component, MAX_PATH_LEN, L"site-packages")) return FALSE;
    if (wcslen(pythonpath) + 1 + wcslen(component) + 1 > sizeof(pythonpath) / sizeof(pythonpath[0])) return FALSE;
    wcscat(pythonpath, L";");
    wcscat(pythonpath, component);
    wcscpy(component, bundle_root);
    if (!path_append(component, MAX_PATH_LEN, L"runtime") || !path_append(component, MAX_PATH_LEN, L"Lib")) return FALSE;
    if (wcslen(pythonpath) + 1 + wcslen(component) + 1 > sizeof(pythonpath) / sizeof(pythonpath[0])) return FALSE;
    wcscat(pythonpath, L";");
    wcscat(pythonpath, component);
    return set_env(L"PYTHONPATH", pythonpath);
}

static size_t quoted_arg_length(const wchar_t *arg) {
    size_t length = 2;
    const wchar_t *cursor;
    size_t backslashes = 0;
    for (cursor = arg; *cursor != L'\0'; cursor++) {
        if (*cursor == L'\\') backslashes++;
        else {
            if (*cursor == L'"') length += backslashes + 1;
            length += backslashes + 1;
            backslashes = 0;
        }
    }
    return length + backslashes * 2;
}

static wchar_t *append_quoted_arg(wchar_t *cursor, const wchar_t *arg) {
    size_t backslashes = 0;
    const wchar_t *source;
    *cursor++ = L'"';
    for (source = arg; *source != L'\0'; source++) {
        if (*source == L'\\') {
            backslashes++;
        } else {
            if (*source == L'"') {
                while (backslashes > 0) {
                    *cursor++ = L'\\';
                    *cursor++ = L'\\';
                    backslashes--;
                }
                *cursor++ = L'\\';
            }
            while (backslashes > 0) {
                *cursor++ = L'\\';
                backslashes--;
            }
            *cursor++ = *source;
        }
    }
    while (backslashes > 0) {
        *cursor++ = L'\\';
        backslashes--;
    }
    *cursor++ = L'"';
    return cursor;
}

static wchar_t *build_command_line(const wchar_t *python_exe, int argc, wchar_t *argv[]) {
    size_t total = quoted_arg_length(python_exe) + 1;
    int index;
    wchar_t *command_line;
    wchar_t *cursor;
    for (index = 1; index < argc; index++) total += 1 + quoted_arg_length(argv[index]);
    command_line = (wchar_t *)malloc(total * sizeof(wchar_t));
    if (command_line == NULL) return NULL;
    cursor = append_quoted_arg(command_line, python_exe);
    wcscpy(cursor, L" -m modulo.cli.main");
    cursor += wcslen(L" -m modulo.cli.main");
    for (index = 1; index < argc; index++) {
        *cursor++ = L' ';
        cursor = append_quoted_arg(cursor, argv[index]);
    }
    *cursor = L'\0';
    return command_line;
}

static BOOL build_path(wchar_t *buffer, size_t buffer_len, const wchar_t *root, const wchar_t *first, const wchar_t *second) {
    if (wcslen(root) >= buffer_len) return FALSE;
    wcscpy(buffer, root);
    if (!path_append(buffer, buffer_len, first)) return FALSE;
    return second == NULL || path_append(buffer, buffer_len, second);
}

int wmain(int argc, wchar_t *argv[]) {
    wchar_t exe_dir[MAX_PATH_LEN];
    wchar_t bundle_root[MAX_PATH_LEN];
    wchar_t path_component[MAX_PATH_LEN];
    wchar_t python_exe[MAX_PATH_LEN];
    wchar_t *last_slash;
    wchar_t *command_line;
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    DWORD exit_code;

    if (!get_executable_dir(exe_dir, MAX_PATH_LEN)) {
        fwprintf(stderr, L"Failed to get executable directory\n");
        return 1;
    }
    wcscpy(bundle_root, exe_dir);
    last_slash = wcsrchr(bundle_root, L'\\');
    if (last_slash == NULL) {
        fwprintf(stderr, L"Unexpected launcher location\n");
        return 1;
    }
    *last_slash = L'\0';
    if (!build_path(path_component, MAX_PATH_LEN, bundle_root, L"bin", NULL) || !set_env(L"MODULO_BUNDLED_BIN_DIR", path_component) || !build_pythonpath(bundle_root)) {
        fwprintf(stderr, L"Failed to construct or set bundle environment\n");
        return 1;
    }
    if (!build_path(path_component, MAX_PATH_LEN, bundle_root, L"runtime", L"bin") || !prepend_path(path_component) || !build_path(path_component, MAX_PATH_LEN, bundle_root, L"pg", L"bin") || !prepend_path(path_component) || !build_path(path_component, MAX_PATH_LEN, bundle_root, L"redis", NULL) || !prepend_path(path_component)) {
        fwprintf(stderr, L"Failed to construct or set PATH\n");
        return 1;
    }
    if (!build_path(python_exe, MAX_PATH_LEN, bundle_root, L"runtime", L"bin") || !path_append(python_exe, MAX_PATH_LEN, L"python3.exe") || GetFileAttributesW(python_exe) == INVALID_FILE_ATTRIBUTES) {
        fwprintf(stderr, L"Python executable not found at runtime\\bin\\python3.exe\n");
        return 1;
    }
    command_line = build_command_line(python_exe, argc, argv);
    if (command_line == NULL) {
        fwprintf(stderr, L"Failed to build Python command line\n");
        return 1;
    }
    startup.cb = sizeof(startup);
    if (!CreateProcessW(python_exe, command_line, NULL, NULL, TRUE, 0, NULL, bundle_root, &startup, &process)) {
        fwprintf(stderr, L"Failed to launch Python (error %lu)\n", GetLastError());
        free(command_line);
        return 1;
    }
    free(command_line);
    WaitForSingleObject(process.hProcess, INFINITE);
    if (!GetExitCodeProcess(process.hProcess, &exit_code)) exit_code = 1;
    CloseHandle(process.hProcess);
    CloseHandle(process.hThread);
    return (int)exit_code;
}
