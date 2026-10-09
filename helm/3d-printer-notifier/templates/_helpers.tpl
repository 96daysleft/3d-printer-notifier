{{/*
Expand the name of the chart.
*/}}
{{- define "3d-printer-notifier.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "3d-printer-notifier.fullname" -}}
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
Common labels
*/}}
{{- define "3d-printer-notifier.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{ include "3d-printer-notifier.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "3d-printer-notifier.selectorLabels" -}}
app.kubernetes.io/name: {{ include "3d-printer-notifier.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Secret holding config.yaml — existingSecret if provided, otherwise the chart-managed one.
*/}}
{{- define "3d-printer-notifier.secretName" -}}
{{- default (include "3d-printer-notifier.fullname" .) .Values.existingSecret }}
{{- end }}

{{/*
The application config.yaml, rendered from values.
*/}}
{{- define "3d-printer-notifier.config" -}}
{{- if not .Values.printers }}
{{- fail "printers: at least one printer must be configured (or set existingSecret)" }}
{{- end }}
{{- $config := dict
  "logLevel" .Values.logLevel
  "logFormat" .Values.logFormat
  "pollIntervalMinutes" .Values.pollIntervalMinutes
  "maxAlertsPerPoll" .Values.maxAlertsPerPoll
  "healthPort" .Values.healthPort
  "discord" .Values.discord
  "notify" .Values.notify
  "printers" .Values.printers
}}
{{- toYaml $config }}
{{- end }}
