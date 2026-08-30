#!/bin/sh
# Render the shared edge CSP snippet (security_headers.conf) from
# security_headers.conf.in, injecting the deployment's configurable monitor
# domains into connect-src so the SPA's edge CSP honours MODULO_MONITOR_DOMAINS
# and the Grafana Faro collector (FAR-447 review Major-1). Mirrors the backend
# SecurityHeadersMiddleware in backend/src/modulo/api/middleware/security_headers.py.
#
# SECURITY: MODULO_MONITOR_DOMAINS / VITE_GRAFANA_FARO_URL are untrusted env
# input on the frontend-only nginx image (which runs no backend validator), so
# render_csp.sh must sanitise them itself. The value is interpolated into a
# DOUBLE-QUOTED add_header string inside an nginx CONFIG FILE, so stripping ';'
# CR LF (as the backend validator does for a plain HTTP header value) is not
# sufficient at the edge:
#   ;        breaks out of the connect-src directive and injects CSP directives
#   CR / LF  break out of the add_header directive entirely
#   "        terminates the quoted string and injects raw nginx config
#   $        is an nginx variable reference; an unknown one is a boot-time
#            `[emerg] unknown "..." variable`, not a silent no-op
#   \        is the nginx escape character
# Rather than blacklist those chars we ALLOWLIST the characters that are valid
# in a CSP source expression and DROP any token containing anything else
# (FAR-447 re-review MAJOR — sanitisation misses '"'). This is strictly
# stronger than backend/src/modulo/settings.py:_validate_monitor_domains.
# We DROP the offending token (rather than reject and exit non-zero) so a
# malformed value degrades to "that origin not added" instead of crash-looping
# boot on the backend-less image.
#
# Reads (all optional):
#   MODULO_MONITOR_DOMAINS  space-separated extra connect-src origins
#   VITE_GRAFANA_FARO_URL   Grafana Faro collector URL (its origin is allowed)
#
# Output: ${OUT} (default /etc/nginx/security_headers.conf), which each nginx
# server config `include`s. Placed OUTSIDE the document root so it is not
# publicly downloadable (FAR-447 re-review MINOR-2).
# -f (noglob) is required: the allowlist below word-splits the env value, and
# tokens legitimately contain '*' (e.g. *.datadoghq.com), which would otherwise
# be pathname-expanded against the CWD.
set -euf

TEMPLATE="${TEMPLATE:-/etc/nginx/security_headers.conf.in}"
OUT="${OUT:-/etc/nginx/security_headers.conf}"

# Defaults mirror the backend middleware's hardcoded allowlist, except that the
# edge also sends `ws: wss:` unconditionally while the backend middleware adds
# them only in debug mode. That divergence is deliberate and documented in
# deploy/nginx/security_headers.conf.in (FAR-447 re-review MINOR).
CONNECT_SRC="*.ingest.sentry.io *.datadoghq.com *.dd.dg *.rum.browserevents.com ws: wss:"

# Emit only those space-separated tokens that consist wholly of characters valid
# in a CSP source expression: alphanumerics . - _ * : /  (enough for
# `*.example.com`, `https:`, `wss:`, `example.com:8443`, `example.com/path`).
# Any token containing anything else -- notably ; " \ $ ` ' ( ) or a control
# char -- is dropped whole. See the SECURITY note in the header.
#
# A bare `*` (and any other use of `*` than a leading `*.` host wildcard) is
# ALSO dropped: `*` is a legal CSP source that silently widens connect-src to
# every host, so letting it through would turn a hostile value like
# `evil.com; script-src *` into an open connect-src even though the ';' itself
# was stripped.
sanitize_tokens() {
    _out=""
    for _tok in $1; do
        case "$_tok" in
            "") continue ;;
            # Character allowlist.
            *[!a-zA-Z0-9.:/*_-]*) continue ;;
        esac
        # Wildcard policy: allow only a leading '*.' and no other '*'.
        case "${_tok#\*.}" in
            *\**) continue ;;
        esac
        _out="$_out $_tok"
    done
    printf '%s' "$_out"
}

extra=""
if [ -n "${MODULO_MONITOR_DOMAINS:-}" ]; then
    extra="$extra$(sanitize_tokens "$MODULO_MONITOR_DOMAINS")"
fi
if [ -n "${VITE_GRAFANA_FARO_URL:-}" ]; then
    # Reduce the collector URL to its origin (host[:port]) before allowlisting.
    faro=$(printf '%s' "$VITE_GRAFANA_FARO_URL" | sed -E 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##; s#/.*##')
    extra="$extra$(sanitize_tokens "$faro")"
fi

# Collapse any runs of spaces left by sanitised-away tokens.
extra=$(printf '%s' "$extra" | tr -s ' ')

if [ -n "$extra" ]; then
    CONNECT_SRC="$CONNECT_SRC$extra"
fi

export CONNECT_SRC
envsubst '${CONNECT_SRC}' < "$TEMPLATE" > "$OUT"
