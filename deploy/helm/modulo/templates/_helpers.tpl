{{/*
Expand the name of the chart.
*/}}
{{- define "modulo.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "modulo.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "modulo.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "modulo.labels" -}}
helm.sh/chart: {{ include "modulo.chart" . }}
{{ include "modulo.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "modulo.selectorLabels" -}}
app.kubernetes.io/name: {{ include "modulo.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Service account name.
*/}}
{{- define "modulo.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "modulo.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Namespace name.
*/}}
{{- define "modulo.namespace" -}}
{{- default .Release.Namespace .Values.namespace.name }}
{{- end }}

{{/*
Resolve a container image reference to a single string. A digest (immutable)
takes precedence over a tag when both are set. Rendering one value prevents the
duplicate `image:` key that breaks strict YAML parsing.
Usage: {{ include "modulo.image" (dict "image" .Values.backend.image) }}
*/}}
{{- define "modulo.image" -}}
{{- if .image.digest -}}
{{- printf "%s@%s" .image.repository .image.digest -}}
{{- else -}}
{{- printf "%s:%s" .image.repository (.image.tag | default "latest") -}}
{{- end -}}
{{- end }}

{{/*
Construct DATABASE_URL from Postgres config.
When postgres.host is set, builds the URL from individual fields.
Otherwise falls back to backend.env.DATABASE_URL.
*/}}
{{- define "modulo.databaseUrl" -}}
{{- if .Values.postgres.host }}
{{- $host := .Values.postgres.host }}
{{- $port := .Values.postgres.port | int }}
{{- $db := .Values.postgres.database }}
{{- $user := .Values.postgres.username }}
{{- $pass := "" }}
{{- if .Values.postgres.existingSecret }}
{{- $secret := lookup "v1" "Secret" (include "modulo.namespace" .) .Values.postgres.existingSecret }}
{{- if $secret }}
{{- $pass = index $secret.data "password" | b64dec }}
{{- end }}
{{- else }}
{{- $pass = .Values.postgres.password }}
{{- end }}
{{- printf "postgresql+asyncpg://%s:%s@%s:%d/%s" $user (urlquery $pass) $host $port $db }}
{{- else }}
{{- .Values.backend.env.DATABASE_URL | default "" }}
{{- end }}
{{- end }}

{{/*
Construct DATABASE_ADMIN_URL from Postgres config.
Used by entrypoint.sh bootstrap_role.py to connect as the admin/superuser
for migrations and role bootstrap. Uses postgres.adminUsername (default: modulo)
rather than postgres.username (modulo_app) — the two must differ so the
app role is NOT the superuser.
When postgres.host is unset, falls back to backend.env.DATABASE_ADMIN_URL.
*/}}
{{- define "modulo.databaseAdminUrl" -}}
{{- if .Values.postgres.host }}
{{- $host := .Values.postgres.host }}
{{- $port := .Values.postgres.port | int }}
{{- $db := .Values.postgres.database }}
{{- $user := .Values.postgres.adminUsername | default "modulo" }}
{{- $pass := "" }}
{{- if .Values.postgres.existingSecret }}
{{- $secret := lookup "v1" "Secret" (include "modulo.namespace" .) .Values.postgres.existingSecret }}
{{- if $secret }}
{{- $pass = index $secret.data "password" | b64dec }}
{{- end }}
{{- else }}
{{- $pass = .Values.postgres.password }}
{{- end }}
{{- printf "postgresql+asyncpg://%s:%s@%s:%d/%s" $user (urlquery $pass) $host $port $db }}
{{- else }}
{{- .Values.backend.env.DATABASE_ADMIN_URL | default "" }}
{{- end }}
{{- end }}

{{/*
Resolve the effective Redis password.
Precedence: redis.password, then the "password" key of redis.existingSecret.
Returns "" when neither yields a value (no auth). Shared by REDIS_URL and the
embedded Redis Deployment so URL credentials and --requirepass never diverge.
*/}}
{{- define "modulo.redisPassword" -}}
{{- if .Values.redis.password -}}
{{- .Values.redis.password -}}
{{- else if .Values.redis.existingSecret -}}
{{- $secret := lookup "v1" "Secret" (include "modulo.namespace" .) .Values.redis.existingSecret -}}
{{- if $secret -}}
{{- index $secret.data "password" | b64dec -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Construct REDIS_URL from Redis config.
When redis.embedded is true, points at the chart's own Redis Service
({{ include "modulo.fullname" . }}-redis on port 6379), embedding the password
when one is configured.
When redis.host is set (external mode), builds the URL from individual fields.
Otherwise falls back to backend.env.REDIS_URL.
*/}}
{{- define "modulo.redisUrl" -}}
{{- $db := .Values.redis.db | int }}
{{- $pass := include "modulo.redisPassword" . }}
{{- if .Values.redis.embedded }}
{{- if $pass }}
{{- printf "redis://:%s@%s-redis:6379/%d" (urlquery $pass) (include "modulo.fullname" .) $db }}
{{- else }}
{{- printf "redis://%s-redis:6379/%d" (include "modulo.fullname" .) $db }}
{{- end }}
{{- else if .Values.redis.host }}
{{- $host := .Values.redis.host }}
{{- $port := .Values.redis.port | int }}
{{- if $pass }}
{{- printf "redis://:%s@%s:%d/%d" (urlquery $pass) $host $port $db }}
{{- else }}
{{- printf "redis://%s:%d/%d" $host $port $db }}
{{- end }}
{{- else }}
{{- .Values.backend.env.REDIS_URL | default "redis://localhost:6379/0" }}
{{- end }}
{{- end }}
