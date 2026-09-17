# E2B Docker Capability Probe

Date: 2026-09-17

## Command Results

### 1. docker --version

```bash
/bin/bash: line 1: docker: command not found
```

Exit code: 127

### 2. docker info

```bash
/bin/bash: line 1: docker: command not found
```

Exit code: 127

### 3. docker ps

```bash
/bin/bash: line 1: docker: command not found
```

Exit code: 127

### 4. command -v docker || echo 'docker not on PATH'

```bash
docker not on PATH
```

Exit code: 0

### 5. ls -la /var/run/docker.sock

```bash
ls: cannot access '/var/run/docker.sock': No such file or directory
```

Exit code: 2

### 6. test -S /var/run/docker.sock && echo 'SOCKET EXISTS' || echo 'NO SOCKET'

```bash
NO SOCKET
```

Exit code: 0

### 7. docker run --rm hello-world

```bash
/bin/bash: line 1: docker: command not found
```

Exit code: 127

### 8. cat /proc/1/cgroup | head -5

```bash
0::/init.scope
```

Exit code: 0

## CONCLUSION

Docker daemon is NOT available in the E2B sandbox
