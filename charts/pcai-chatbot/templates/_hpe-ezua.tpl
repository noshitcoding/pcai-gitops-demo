{{/* Labels AI Essentials uses for health, metering and scheduling of imported frameworks */}}
{{- define "hpe-ezua.labels" -}}
hpe-ezua/app: {{ .Release.Name }}
hpe-ezua/type: vendor-service
hpe-ezua/component: chatbot
{{- end }}

{{- define "pcai-chatbot.selector" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
