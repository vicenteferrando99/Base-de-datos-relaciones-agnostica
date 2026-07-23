{{/* Nombre base del chart */}}
{{- define "cloud-db-api.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Nombre completo (release + chart), truncado al límite de K8s */}}
{{- define "cloud-db-api.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "cloud-db-api.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Labels estándar recomendadas por Helm/K8s */}}
{{- define "cloud-db-api.labels" -}}
app.kubernetes.io/name: {{ include "cloud-db-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Labels de selección (subconjunto estable de las anteriores) */}}
{{- define "cloud-db-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cloud-db-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
