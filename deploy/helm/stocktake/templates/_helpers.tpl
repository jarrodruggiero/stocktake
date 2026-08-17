{{- define "stocktake.name" -}}{{ default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}{{- end -}}

{{- define "stocktake.fullname" -}}
{{- if .Values.fullnameOverride -}}{{ .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-%s" .Release.Name (include "stocktake.name" .) | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- end -}}

{{- define "stocktake.labels" -}}
app.kubernetes.io/name: {{ include "stocktake.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "stocktake.selectorLabels" -}}
app.kubernetes.io/name: {{ include "stocktake.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
