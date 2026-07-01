{{- /*
modulo.helpers.tpl - Helper templates for the Modulo Helm chart
*/}}

{{- /*
Expand the name of the chart.
*/}}
{{- define "modulo.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- /*
Create a default fully qualified app name.
Truncated at 63 chars because some Kubernetes name fields are limited to 63.
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

{{- /*
Backend fullname.
*/}}
{{- define "modulo.backend.fullname" -}}
{{- printf "%s-backend" (include "modulo.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- /*
Frontend fullname.
*/}}
{{- define "modulo.frontend.fullname" -}}
{{- printf "%s-frontend" (include "modulo.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- /*
Create chart name and version as used by the chart label.
*/}}
{{- define "modulo.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- /*
Common labels
*/}}
{{- define "modulo.labels" -}}
helm.sh/chart: {{ include "modulo.chart" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- /*
Selector labels
*/}}
{{- define "modulo.backend.selectorLabels" -}}
app.kubernetes.io/name: {{ include "modulo.name" . }}-backend
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "modulo.frontend.selectorLabels" -}}
app.kubernetes.io/name: {{ include "modulo.name" . }}-frontend
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- /*
Image reference helper
*/}}
{{- define "modulo.backend.image" -}}
{{- $registry := .Values.backend.image.registry | default .Values.global.imageRegistry }}
{{- $repository := .Values.backend.image.repository }}
{{- $tag := .Values.backend.image.tag | default .Values.image.tag | default .Chart.AppVersion }}
{{- if $registry }}
{{- printf "%s/%s:%s" $registry $repository $tag }}
{{- else }}
{{- printf "%s:%s" $repository $tag }}
{{- end }}
{{- end }}

{{- define "modulo.frontend.image" -}}
{{- $registry := .Values.frontend.image.registry | default .Values.global.imageRegistry }}
{{- $repository := .Values.frontend.image.repository }}
{{- $tag := .Values.frontend.image.tag | default .Values.image.tag | default .Chart.AppVersion }}
{{- if $registry }}
{{- printf "%s/%s:%s" $registry $repository $tag }}
{{- else }}
{{- printf "%s:%s" $repository $tag }}
{{- end }}
{{- end }}

{{- /*
Return the proper PostgreSQL connection URL.
Uses sub-chart generated secret if available, else falls back to provided DATABASE_URL.
*/}}
{{- define "modulo.databaseUrl" -}}
{{- if .Values.secrets.DATABASE_URL }}
{{- .Values.secrets.DATABASE_URL }}
{{- else if .Values.postgresql.enabled }}
{{- $host := printf "%s-postgresql.%s.svc.cluster.local" .Release.Name .Release.Namespace }}
{{- $port := "5432" }}
{{- $db := .Values.postgresql.auth.database }}
{{- $user := .Values.postgresql.auth.username }}
{{- printf "postgresql://%s:$(DATABASE_PASSWORD)@%s:%s/%s" $user $host $port $db }}
{{- else }}
{{- fail "Either set secrets.DATABASE_URL or enable postgresql.enabled=true" }}
{{- end }}
{{- end }}

{{- /*
Return the proper Redis connection URL.
*/}}
{{- define "modulo.redisUrl" -}}
{{- if .Values.secrets.REDIS_URL }}
{{- .Values.secrets.REDIS_URL }}
{{- else if .Values.redis.enabled }}
{{- $host := printf "%s-redis-master.%s.svc.cluster.local" .Release.Name .Release.Namespace }}
{{- $port := "6379" }}
{{- printf "redis://:$(REDIS_PASSWORD)@%s:%s" $host $port }}
{{- else }}
{{- fail "Either set secrets.REDIS_URL or enable redis.enabled=true" }}
{{- end }}
{{- end }}

{{- /*
Return the appropriate apiVersion for Ingress.
*/}}
{{- define "modulo.ingress.apiVersion" -}}
{{- if and (.Capabilities.APIVersions.Has "networking.k8s.io/v1") (semverCompare ">=1.19-0" .Capabilities.KubeVersion.Version) }}
{{- print "networking.k8s.io/v1" }}
{{- else if .Capabilities.APIVersions.Has "networking.k8s.io/v1beta1" }}
{{- print "networking.k8s.io/v1beta1" }}
{{- else }}
{{- print "extensions/v1beta1" }}
{{- end }}
{{- end }}

{{- /*
Return the ingress path format depending on the API version.
*/}}
{{- define "modulo.ingress.pathType" -}}
{{- if semverCompare ">=1.18-0" $.Capabilities.KubeVersion.Version }}
{{- print "Prefix" }}
{{- end }}
{{- end }}

{{- /*
Secret name for backend secrets.
*/}}
{{- define "modulo.backend.secretName" -}}
{{- if .Values.backend.existingSecret }}
{{- .Values.backend.existingSecret }}
{{- else }}
{{- printf "%s-backend-secrets" (include "modulo.fullname" .) }}
{{- end }}
{{- end }}

{{- /*
ServiceAccount name for the global service account.
*/}}
{{- define "modulo.serviceAccountName" -}}
{{- if .Values.serviceAccount.name }}
{{- .Values.serviceAccount.name }}
{{- else }}
{{- printf "%s-sa" (include "modulo.fullname" .) }}
{{- end }}
{{- end }}

{{- /*
ServiceAccount name for the backend.
*/}}
{{- define "modulo.backend.serviceAccountName" -}}
{{- if .Values.backend.serviceAccount.name }}
{{- .Values.backend.serviceAccount.name }}
{{- else if .Values.serviceAccount.create }}
{{- include "modulo.serviceAccountName" . }}
{{- else }}
{{- "default" }}
{{- end }}
{{- end }}

{{- /*
ServiceAccount name for the frontend.
*/}}
{{- define "modulo.frontend.serviceAccountName" -}}
{{- if .Values.frontend.serviceAccount.name }}
{{- .Values.frontend.serviceAccount.name }}
{{- else if .Values.serviceAccount.create }}
{{- include "modulo.serviceAccountName" . }}
{{- else }}
{{- "default" }}
{{- end }}
{{- end }}

{{- /*
nginx ConfigMap name
*/}}
{{- define "modulo.nginx.configMapName" -}}
{{- printf "%s-nginx" (include "modulo.fullname" .) }}
{{- end }}

{{- /*
Render the nginx default.conf content.
*/}}
{{- /*
Return the full service name for an ingress backend by service type.
Usage: {{ include "modulo.ingress.backendName" (dict "ctx" $ "service" "frontend") }}
*/}}
{{- define "modulo.ingress.backendName" -}}
{{- if eq .service "frontend" }}
{{- include "modulo.frontend.fullname" .ctx }}
{{- else }}
{{- include "modulo.backend.fullname" .ctx }}
{{- end }}
{{- end }}

{{- define "modulo.nginx.config" -}}
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://{{ include "modulo.backend.fullname" . }}:{{ .Values.backend.service.port }};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
{{- end }}
