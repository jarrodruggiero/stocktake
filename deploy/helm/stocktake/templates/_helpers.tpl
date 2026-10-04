{{- define "stocktake.name" -}}{{ default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}{{- end -}}

{{- define "stocktake.fullname" -}}
{{- if .Values.fullnameOverride -}}{{ .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-%s" .Release.Name (include "stocktake.name" .) | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- end -}}

{{- define "stocktake.labels" -}}
app.kubernetes.io/name: {{ include "stocktake.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ include "stocktake.imageTag" . | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "stocktake.selectorLabels" -}}
app.kubernetes.io/name: {{ include "stocktake.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- /*
The image tag: one set by hand, else the release this chart was packaged for.
This directory is the source the release packages and names no release, so
installed from a clone it stops here rather than run an image it was not given.
*/ -}}
{{- define "stocktake.imageTag" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if not $tag -}}
{{- fail "This copy of the chart names no release. Install the published chart instead: helm install stocktake oci://ghcr.io/jarrodruggiero/charts/stocktake. Or set image.tag to the release you want." -}}
{{- end -}}
{{- $tag -}}
{{- end -}}
