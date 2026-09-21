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
{{- printf "postgresql+asyncpg://%s:%s@%s:%d/%s" $user $pass $host $port $db }}
{{- else }}
{{- .Values.backend.env.DATABASE_URL | default "" }}
{{- end }}
{{- end }}

{{/*
Construct REDIS_URL from Redis config.
When redis.embedded is true, points at the chart's own Redis Service
({{ include "modulo.fullname" . }}-redis on port 6379).
When redis.host is set (external mode), builds the URL from individual fields.
Otherwise falls back to backend.env.REDIS_URL.
*/}}
{{- define "modulo.redisUrl" -}}
{{- if .Values.redis.embedded }}
{{- $db := .Values.redis.db | int }}
{{- printf "redis://%s-redis:6379/%d" (include "modulo.fullname" .) $db }}
{{- else if .Values.redis.host }}
{{- $host := .Values.redis.host }}
{{- $port := .Values.redis.port | int }}
{{- $db := .Values.redis.db | int }}
{{- $pass := "" }}
{{- if .Values.redis.existingSecret }}
{{- $secret := lookup "v1" "Secret" (include "modulo.namespace" .) .Values.redis.existingSecret }}
{{- if $secret }}
{{- $pass = index $secret.data "password" | b64dec }}
{{- end }}
{{- else }}
{{- $pass = .Values.redis.password }}
{{- end }}
{{- if $pass }}
{{- printf "redis://:%s@%s:%d/%d" $pass $host $port $db }}
{{- else }}
{{- printf "redis://%s:%d/%d" $host $port $db }}
{{- end }}
{{- else }}
{{- .Values.backend.env.REDIS_URL | default "redis://localhost:6379/0" }}
{{- end }}
{{- end }}
