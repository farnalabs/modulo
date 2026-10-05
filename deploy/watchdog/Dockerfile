# Modulo health watchdog image - the `watchdog` service in docker-compose.yml.
#
# Why a derived image exists at all: upstream `twinproduction/gatus` is built
# FROM scratch - no shell, no wget, no curl. Verified against v5.37.0 before
# this file was written:
#
#   $ docker run --rm --entrypoint /bin/sh twinproduction/gatus:v5.37.0 -c 'echo hi'
#   exec: "/bin/sh": stat /bin/sh: no such file or directory   (exit 127)
#
# That rules out an in-container HTTP healthcheck and any startup line of our
# own, so this image re-hosts the same upstream /gatus binary on Alpine, which
# supplies both (all three properties of alpine:3.24 verified by running it):
#
#   * /bin/busybox -> the `sh` entrypoint needs it and the compose healthcheck
#     execs it directly as `/bin/busybox wget ...` (healthcheck CMD does not go
#     through ENTRYPOINT);
#   * the busybox `wget` applet, for that healthcheck;
#   * /etc/ssl/certs/ca-certificates.crt, so Gatus can verify an SMTP STARTTLS
#     peer - no `apk add` and therefore no network dependency at build time.
#
# Bases are pinned. The Gatus tag was verified against the Docker Hub tags API
# for twinproduction/gatus: v5.37.0 published 2026-09-24 (newest at the time;
# `stable`/`latest` deliberately unused - they float).
FROM twinproduction/gatus:v5.37.0 AS upstream

FROM alpine:3.24

# Run unprivileged. Nothing here needs root: /gatus is static and binds the
# unprivileged port 8080, the config is mounted/baked read-only, and Gatus
# persists to disk only when a `storage` backend is configured (none is).
# Dropping root also clears the "alpine base runs as root" static finding
# (docker:S6471). The user is created with busybox adduser/addgroup, so there is
# still no `apk add` and no network dependency beyond the two base images.
RUN addgroup -S gatus && adduser -S -G gatus -H -s /sbin/nologin gatus

# /gatus is a static Go binary (upstream's own base is scratch), so it runs
# unchanged on Alpine.
COPY --from=upstream /gatus /gatus

# entrypoint.sh prints the one-line alerting on/off statement, then execs
# /gatus so it becomes PID 1 and takes signals directly. config.yaml is baked
# in so a bare `docker run` monitors Modulo rather than upstream's bundled demo
# endpoints; docker-compose.yml mounts the checked-in file over this path so
# edits apply without a rebuild.
COPY entrypoint.sh /entrypoint.sh
COPY config.yaml /config/config.yaml

# Mirror the env upstream's image declares (upstream sets an empty
# GATUS_CONFIG_PATH, which means "use the default config/config.yaml").
ENV GATUS_LOG_LEVEL=INFO \
    GATUS_CONFIG_PATH= \
    PORT=8080

WORKDIR /

# Drop privileges for the runtime process (see the adduser note above).
USER gatus

ENTRYPOINT ["/bin/busybox", "sh", "/entrypoint.sh"]
