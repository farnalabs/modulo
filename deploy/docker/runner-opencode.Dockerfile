# syntax=docker/dockerfile:1
#
# modulo-runner:opencode — the Bundled Runner first-party image (FAR-590, D4).
#
# Contains everything a Modulo sandbox_agent node needs to execute opencode
# (or script-mode commands) inside a hardened workspace container:
#   - git + common CLIs a script mode run may need
#   - opencode (version pinned via the OPENCODE_VERSION build arg — the
#     release job advances it and republishes; see "Pin-advance process" below)
#
# Pin-advance process (ADR 029 / plan D4):
#   1. The GHCR publish release job (GA item) bumps OPENCODE_VERSION to the
#      current minor, builds, then scans (trivy/grype fail high/critical)
#      and runs container-structure-test before publishing the digest.
#   2. The published tag advances the per-minor `released-<minor>` tag AND
#     updates the digest constant (`BUNDLED_RUNNER_IMAGE_REF` in
#     backend/src/modulo/db/bundled_runner_template.py). The release job's
#     digest-drift guard asserts the constant matches `released-<minor>`.
#   3. Support window: the image supports the CURRENT opencode minor and
#     N-1. A pin advance that moves past N-1 is a release-note item.
#   4. Digests below are release-advanced: the D4 seed constants pin the
#     per-minor digest, and an operator-pinned older digest survives
#     (template updates are surfaced for review, never silently applied —
#     the live drift helper in the profile module).

# FROM digest-pinned base (FAR-590 D4 blocking criterion). debian:12-slim keeps
# the image small while glibc keeps opencode + python wheels happy. The digest
# below is release-advanced by the publish job; bump it only when the base
# image is re-scanned and the seed/docs digest constants advance with it.
# Pin by DIGEST ONLY (no :tag) so the base image reference is unambiguous and
# cannot silently drift to a different tag.
ARG BASE_IMAGE=debian@sha256:f324c7ff54321e8d9c588493a20244965938ce0aa50bbd1022d38010e9ffc4b1

FROM ${BASE_IMAGE}

ARG OPENCODE_VERSION=1.18.29
ARG NODE_MAJOR=22
ARG OPENCODE_ARCH=x64

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    EDITOR=/bin/vi

# Common CLIs: git (runner identity separation), curl/wget for probes,
# unzip/xz for installs, tini for signal-correct PID 1, vi for interactive
# debugging, procps for ps-based diagnostics in script mode.
# ca-certificates before anything that talks TLS.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gnupg2 \
    jq \
    openssh-client \
    procps \
    tini \
    unzip \
    vim-tiny \
    wget \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# opencode needs Node (the vendored runtime is a JS CLI). Install Node LTS from
# NodeSource, then the pinned opencode binary via npm. The NodeSource setup
# script is a downloaded artifact and MUST NOT be executed (SonarCloud S5038:
# executing downloaded artifacts without verification). Instead the official
# NodeSource signing key + apt source are added directly, so no downloaded
# script ever runs inside the build. npm installs with --ignore-scripts so no
# lifecycle scripts from the published tarball run.
RUN curl --proto '=https' --tlsv1.2 -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g --ignore-scripts "opencode-ai@${OPENCODE_VERSION}" \
    && groupadd -g 1001 runner && useradd -m -u 1001 -g runner -s /bin/bash runner

# Non-root user (ADR 029 workspace hardening): uid 1001, writable session dirs
# are handed to the container as tmpfs mounts on workdir/$HOME (the provider
# mounts tmpfs at provision), so the image itself stays immutable.
USER runner
WORKDIR /home/user
ENV HOME=/home/user \
    PATH=/usr/local/bin:/usr/bin:/bin

# The workspace entrypoint is `sleep infinity` (the provider keeps the
# container alive for exec sessions). tini keeps signal handling correct for
# the sleep process, so containers are cleanly stoppable mid-exec.
ENTRYPOINT ["/usr/bin/tini", "--", "sleep", "infinity"]

LABEL org.opencontainers.image.base.name="debian:12-slim" \
    org.opencontainers.image.description="Modulo Bundled Runner workspace image (opencode)" \
    org.opencontainers.image.licenses="BUSL-1.1" \
    modulo.runner.minor-support="current+n-1"
