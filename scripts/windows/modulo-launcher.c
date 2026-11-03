/* Windows native launcher for the Modulo single-install bundle. */
/* The launcher uses the checked-by-construction wide-string helpers below and
 * builds with /WX; MSVC's C4996 deprecation warning would otherwise fail it. */
#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <wchar.h>

#define MAX_PATH_LEN 32767
#define ENVIRONMENT_BLOCK_LEN 131072

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

static BOOL build_path(wchar_t *buffer, size_t buffer_len, const wchar_t *root, const wchar_t *first, const wchar_t *second) {
    if (wcslen(root) >= buffer_len) return FALSE;
    wcscpy(buffer, root);
    if (!path_append(buffer, buffer_len, first)) return FALSE;
    return second == NULL || path_append(buffer, buffer_len, second);
}

#define ENV_READ_ERROR (-1)
#define ENV_READ_SKIP 0
#define ENV_READ_FOUND 1

/* Returns ENV_READ_FOUND with a value, ENV_READ_SKIP when an optional variable
 * is absent or empty, or ENV_READ_ERROR when a required variable is unusable.
 * A present-but-empty variable returns zero from GetEnvironmentVariableW while
 * GetLastError stays ERROR_SUCCESS, so length alone cannot distinguish it from
 * an absent variable. */
static int read_inherited_env(const wchar_t *name, wchar_t *value, size_t value_len, BOOL required) {
    DWORD length;
    DWORD error;
    SetLastError(ERROR_SUCCESS);
    length = GetEnvironmentVariableW(name, value, (DWORD)value_len);
    if (length == 0) {
        error = GetLastError();
        if (error == ERROR_ENVVAR_NOT_FOUND || error == ERROR_SUCCESS) {
            /* Absent or present-but-empty: skip optional, reject required. */
            return required ? ENV_READ_ERROR : ENV_READ_SKIP;
        }
        return ENV_READ_ERROR;
    }
    return length < value_len ? ENV_READ_FOUND : ENV_READ_ERROR;
}

static BOOL append_environment_entry(wchar_t *block, size_t block_len, size_t *used, const wchar_t *name, const wchar_t *value) {
    size_t name_len = wcslen(name);
    size_t value_len = wcslen(value);
    size_t entry_len = name_len + 1 + value_len + 1;
    if (*used + entry_len + 1 > block_len) return FALSE;
    wmemcpy(block + *used, name, name_len);
    *used += name_len;
    block[(*used)++] = L'=';
    wmemcpy(block + *used, value, value_len);
    *used += value_len;
    block[(*used)++] = L'\0';
    block[*used] = L'\0';
    return TRUE;
}

static BOOL append_inherited_env(wchar_t *block, size_t block_len, size_t *used, const wchar_t *name, BOOL required) {
    wchar_t value[ENVIRONMENT_BLOCK_LEN];
    int status = read_inherited_env(name, value, sizeof(value) / sizeof(value[0]), required);
    if (status == ENV_READ_SKIP) return TRUE;
    if (status == ENV_READ_ERROR) return FALSE;
    return append_environment_entry(block, block_len, used, name, value);
}

/*
 * Build the child environment instead of inheriting the caller's environment.
 * The allowlist deliberately excludes interpreter/DLL and certificate controls
 * such as PYTHONHOME, PYTHONUSERBASE, OPENSSL_CONF, SSL_CERT_FILE,
 * SSL_CERT_DIR, REQUESTS_CA_BUNDLE, and CURL_CA_BUNDLE. TLS uses the Windows
 * certificate store; the bundled runtime owns every executable search path.
 * Identity and Windows service plumbing remain available to the child.
 */
static BOOL build_clean_environment(const wchar_t *bundle_root, const wchar_t *bundle_bin_dir, const wchar_t *python_path, const wchar_t *path_value, wchar_t **environment) {
    static const wchar_t *const inherited_allowlist[] = {
        L"SYSTEMROOT", L"WINDIR", L"TEMP", L"TMP", L"USERPROFILE", L"APPDATA", L"LOCALAPPDATA",
        L"PROGRAMDATA", L"ALLUSERSPROFILE", L"HOMEDRIVE", L"HOMEPATH", L"USERNAME", L"USERDOMAIN",
        L"USERDOMAIN_ROAMINGPROFILE", L"COMPUTERNAME", L"LOGONSERVER", L"SESSIONNAME", L"NUMBER_OF_PROCESSORS",
        L"PROCESSOR_ARCHITECTURE", L"PROCESSOR_IDENTIFIER", L"PROCESSOR_LEVEL", L"PROCESSOR_REVISION",
        L"OS", L"PATHEXT", L"COMSPEC", L"SERVICE_NAME", L"SERVICE_ID", L"SERVICE_INSTANCE_ID"
    };
    wchar_t *block = (wchar_t *)calloc(ENVIRONMENT_BLOCK_LEN, sizeof(wchar_t));
    size_t used = 0;
    size_t index;
    wchar_t python_home[MAX_PATH_LEN];
    if (block == NULL) return FALSE;
    for (index = 0; index < sizeof(inherited_allowlist) / sizeof(inherited_allowlist[0]); index++) {
        BOOL required = wcscmp(inherited_allowlist[index], L"SYSTEMROOT") == 0 || wcscmp(inherited_allowlist[index], L"WINDIR") == 0;
        if (!append_inherited_env(block, ENVIRONMENT_BLOCK_LEN, &used, inherited_allowlist[index], required)) {
            free(block);
            return FALSE;
        }
    }
    if (!build_path(python_home, MAX_PATH_LEN, bundle_root, L"runtime", L"python") ||
        !append_environment_entry(block, ENVIRONMENT_BLOCK_LEN, &used, L"PATH", path_value) ||
        !append_environment_entry(block, ENVIRONMENT_BLOCK_LEN, &used, L"PYTHONPATH", python_path) ||
        !append_environment_entry(block, ENVIRONMENT_BLOCK_LEN, &used, L"PYTHONHOME", python_home) ||
        !append_environment_entry(block, ENVIRONMENT_BLOCK_LEN, &used, L"MODULO_BUNDLED_BIN_DIR", bundle_bin_dir)) {
        free(block);
        return FALSE;
    }
    *environment = block;
    return TRUE;
}

static BOOL checked_add_size(size_t *total, size_t value) {
    if (value > SIZE_MAX - *total) return FALSE;
    *total += value;
    return TRUE;
}

static BOOL quoted_arg_length(const wchar_t *arg, size_t *length) {
    size_t result = 2;
    const wchar_t *cursor;
    size_t backslashes = 0;
    for (cursor = arg; *cursor != L'\0'; cursor++) {
        if (*cursor == L'\\') {
            if (backslashes == SIZE_MAX) return FALSE;
            backslashes++;
        } else {
            size_t escaped_segment;
            if (backslashes == SIZE_MAX) return FALSE;
            escaped_segment = backslashes + 1;
            if (*cursor == L'"' && !checked_add_size(&result, escaped_segment)) return FALSE;
            if (!checked_add_size(&result, escaped_segment)) return FALSE;
            backslashes = 0;
        }
    }
    if (backslashes > (SIZE_MAX - result) / 2) return FALSE;
    *length = result + backslashes * 2;
    return TRUE;
}

/* Emit a pending run of backslashes verbatim, as a literal character needs. */
static wchar_t *emit_backslashes(wchar_t *cursor, size_t backslashes) {
    while (backslashes > 0) {
        *cursor++ = L'\\';
        backslashes--;
    }
    return cursor;
}

/* Emit a pending run of backslashes doubled, as a literal quote requires so the
 * child's argument parser reads the run back verbatim. */
static wchar_t *emit_doubled_backslashes(wchar_t *cursor, size_t backslashes) {
    while (backslashes > 0) {
        *cursor++ = L'\\';
        *cursor++ = L'\\';
        backslashes--;
    }
    return cursor;
}

static wchar_t *append_quoted_arg(wchar_t *cursor, const wchar_t *arg) {
    size_t backslashes = 0;
    *cursor++ = L'"';
    for (const wchar_t *source = arg; *source != L'\0'; source++) {
        if (*source == L'\\') {
            backslashes++;
            continue;
        }
        if (*source == L'"') {
            cursor = emit_doubled_backslashes(cursor, backslashes);
            *cursor++ = L'\\';
            backslashes = 0;
        }
        cursor = emit_backslashes(cursor, backslashes);
        backslashes = 0;
        *cursor++ = *source;
    }
    cursor = emit_backslashes(cursor, backslashes);
    *cursor++ = L'"';
    return cursor;
}

static wchar_t *build_command_line(const wchar_t *python_exe, int argc, wchar_t *argv[]) {
    static const wchar_t module_invocation[] = L" -m modulo.cli.main";
    size_t total = 0;
    size_t argument_length;
    int index;
    wchar_t *command_line;
    wchar_t *cursor;

    if (!quoted_arg_length(python_exe, &argument_length) ||
        !checked_add_size(&total, argument_length) ||
        !checked_add_size(&total, wcslen(module_invocation)) ||
        !checked_add_size(&total, 1)) {
        return NULL;
    }
    for (index = 1; index < argc; index++) {
        if (!quoted_arg_length(argv[index], &argument_length) ||
            !checked_add_size(&total, argument_length) ||
            !checked_add_size(&total, 1)) {
            return NULL;
        }
    }
    if (total > SIZE_MAX / sizeof(wchar_t)) return NULL;
    command_line = (wchar_t *)malloc(total * sizeof(wchar_t));
    if (command_line == NULL) return NULL;
    cursor = append_quoted_arg(command_line, python_exe);
    wcscpy(cursor, module_invocation);
    cursor += wcslen(module_invocation);
    for (index = 1; index < argc; index++) {
        *cursor++ = L' ';
        cursor = append_quoted_arg(cursor, argv[index]);
    }
    *cursor = L'\0';
    return command_line;
}

static BOOL build_pythonpath(const wchar_t *bundle_root, wchar_t *pythonpath, size_t pythonpath_len) {
    wchar_t component[MAX_PATH_LEN];
    if (wcslen(bundle_root) >= MAX_PATH_LEN) return FALSE;
    wcscpy(component, bundle_root);
    if (!path_append(component, MAX_PATH_LEN, L"backend") || !path_append(component, MAX_PATH_LEN, L"src")) return FALSE;
    if (wcslen(component) + 1 > pythonpath_len) return FALSE;
    wcscpy(pythonpath, component);
    wcscpy(component, bundle_root);
    if (!path_append(component, MAX_PATH_LEN, L"runtime") || !path_append(component, MAX_PATH_LEN, L"python") || !path_append(component, MAX_PATH_LEN, L"Lib") || !path_append(component, MAX_PATH_LEN, L"site-packages")) return FALSE;
    if (wcslen(pythonpath) + 1 + wcslen(component) + 1 > pythonpath_len) return FALSE;
    wcscat(pythonpath, L";");
    wcscat(pythonpath, component);
    wcscpy(component, bundle_root);
    if (!path_append(component, MAX_PATH_LEN, L"runtime") || !path_append(component, MAX_PATH_LEN, L"Lib")) return FALSE;
    if (wcslen(pythonpath) + 1 + wcslen(component) + 1 > pythonpath_len) return FALSE;
    wcscat(pythonpath, L";");
    wcscat(pythonpath, component);
    return TRUE;
}

int wmain(int argc, wchar_t *argv[]) {
    /* These path buffers total ~1 MB; keeping them on the default 1 MB stack
     * overflows it. The launcher is single-threaded, so make them static. */
    static wchar_t exe_dir[MAX_PATH_LEN];
    static wchar_t bundle_root[MAX_PATH_LEN];
    static wchar_t bundle_bin_dir[MAX_PATH_LEN];
    static wchar_t runtime_bin[MAX_PATH_LEN];
    static wchar_t pg_bin[MAX_PATH_LEN];
    static wchar_t redis_dir[MAX_PATH_LEN];
    static wchar_t path_value[MAX_PATH_LEN * 4];
    static wchar_t pythonpath[MAX_PATH_LEN * 4];
    static wchar_t python_exe[MAX_PATH_LEN];
    wchar_t *last_slash;
    wchar_t *command_line;
    wchar_t *environment = NULL;
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
    if (!build_path(bundle_bin_dir, MAX_PATH_LEN, bundle_root, L"bin", NULL) ||
        !build_path(runtime_bin, MAX_PATH_LEN, bundle_root, L"runtime", L"bin") ||
        !build_path(pg_bin, MAX_PATH_LEN, bundle_root, L"pg", L"bin") ||
        !build_path(redis_dir, MAX_PATH_LEN, bundle_root, L"redis", NULL) ||
        wcslen(redis_dir) + 1 + wcslen(pg_bin) + 1 + wcslen(runtime_bin) + 1 > sizeof(path_value) / sizeof(path_value[0]) ||
        !build_pythonpath(bundle_root, pythonpath, sizeof(pythonpath) / sizeof(pythonpath[0]))) {
        fwprintf(stderr, L"Failed to construct bundle paths\n");
        return 1;
    }
    wcscpy(path_value, redis_dir);
    wcscat(path_value, L";");
    wcscat(path_value, pg_bin);
    wcscat(path_value, L";");
    wcscat(path_value, runtime_bin);
    if (!build_path(python_exe, MAX_PATH_LEN, bundle_root, L"runtime", L"bin") || !path_append(python_exe, MAX_PATH_LEN, L"python3.exe") || GetFileAttributesW(python_exe) == INVALID_FILE_ATTRIBUTES) {
        fwprintf(stderr, L"Python executable not found at runtime\\bin\\python3.exe (runtime/python must contain the complete PBS tree)\n");
        return 1;
    }
    if (!build_clean_environment(bundle_root, bundle_bin_dir, pythonpath, path_value, &environment)) {
        fwprintf(stderr, L"Failed to construct the clean bundled runtime environment\n");
        return 1;
    }
    command_line = build_command_line(python_exe, argc, argv);
    if (command_line == NULL) {
        fwprintf(stderr, L"Failed to build Python command line\n");
        free(environment);
        return 1;
    }
    startup.cb = sizeof(startup);
    if (!CreateProcessW(python_exe, command_line, NULL, NULL, TRUE, CREATE_UNICODE_ENVIRONMENT, environment, bundle_root, &startup, &process)) {
        fwprintf(stderr, L"Failed to launch Python (error %lu)\n", GetLastError());
        free(command_line);
        free(environment);
        return 1;
    }
    free(command_line);
    free(environment);
    WaitForSingleObject(process.hProcess, INFINITE);
    if (!GetExitCodeProcess(process.hProcess, &exit_code)) exit_code = 1;
    CloseHandle(process.hProcess);
    CloseHandle(process.hThread);
    return (int)exit_code;
}
