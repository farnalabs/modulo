# Windows-native launcher contract

The native launcher keeps the existing SAQ worker commands and the bundled
PostgreSQL/Redis loopback contracts unchanged. This slice adds the platform
seam used by launcher code and keeps Linux and macOS implementations intact.

## Supported runtime contract

- The Windows data directory is `%PROGRAMDATA%\Modulo\data`. The launcher
  fails closed when `PROGRAMDATA` is unavailable rather than choosing an
  unexpected user directory.
- Bundled executable names use the `.exe` suffix on Windows. The launcher does
  not import Windows-only modules on Linux.
- Windows PostgreSQL bootstrap uses the invariant `C` locale rather than the
  POSIX-only `C.UTF-8` name. The shipped bundled PostgreSQL build must still be
  tested on Windows to confirm that the selected locale and encoding behave as
  required.
- Bundled PostgreSQL and Redis remain launcher-owned child processes on
  loopback ports persisted in `state.json`; SAQ remains launched with the
  existing `python -m saq ...` commands.
- POSIX process groups, `flock`, `chmod 0600`, and POSIX signal semantics are
  not treated as Windows implementations. Windows locking, child teardown,
  secret-file ACL enforcement, and service lifecycle require Windows-native
  implementations or an explicit unsupported error.

## Validation boundary

Linux unit tests validate the platform decisions and preserve the POSIX paths.
They cannot validate Windows named mutexes, Job Objects, Task Scheduler, ACLs,
or the actual bundled PostgreSQL/Redis binaries. A Windows validation pass is
still required with the real installer, bundled binaries, first boot, restart,
stop, upgrade, and service-install flows before Windows-native support is
called complete.
