{{/*
Expand the name of the chart.
*/}}
{{- define "landppt.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "landppt.fullname" -}}
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
Chart label.
*/}}
{{- define "landppt.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "landppt.labels" -}}
helm.sh/chart: {{ include "landppt.chart" . }}
{{ include "landppt.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "landppt.selectorLabels" -}}
app.kubernetes.io/name: {{ include "landppt.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Secret containing the internal PostgreSQL password.
*/}}
{{- define "landppt.postgresqlSecretName" -}}
{{- if .Values.postgresql.auth.existingSecret -}}
{{ .Values.postgresql.auth.existingSecret }}
{{- else -}}
{{ include "landppt.fullname" . }}-postgresql
{{- end -}}
{{- end }}

{{/*
Key containing the internal PostgreSQL password.
*/}}
{{- define "landppt.postgresqlPasswordKey" -}}
{{- .Values.postgresql.auth.passwordKey | default "POSTGRES_PASSWORD" -}}
{{- end }}

{{/*
Database URL for cases where it is safe to render directly into a Helm-managed Secret.
For production, prefer existingSecret-based injection instead.
*/}}
{{- define "landppt.databaseUrl" -}}
{{- if .Values.postgresql.enabled -}}
postgresql://{{ .Values.postgresql.auth.username }}:{{ required "postgresql.auth.password is required when postgresql.enabled=true and postgresql.auth.existingSecret is empty" .Values.postgresql.auth.password }}@{{ include "landppt.fullname" . }}-postgresql:5432/{{ .Values.postgresql.auth.database }}
{{- else if .Values.externalDatabase.existingSecret -}}
{{- printf "" -}}
{{- else -}}
{{ required "externalDatabase.url or externalDatabase.existingSecret is required when postgresql.enabled=false" .Values.externalDatabase.url }}
{{- end -}}
{{- end }}

{{/*
DATABASE_URL environment variables without exposing internal PostgreSQL passwords in rendered manifests.
*/}}
{{- define "landppt.databaseEnv" -}}
{{- if .Values.postgresql.enabled }}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "landppt.postgresqlSecretName" . }}
      key: {{ include "landppt.postgresqlPasswordKey" . }}
- name: DATABASE_URL
  value: {{ printf "postgresql://%s:$(POSTGRES_PASSWORD)@%s-postgresql:5432/%s" .Values.postgresql.auth.username (include "landppt.fullname" .) .Values.postgresql.auth.database | quote }}
{{- else if .Values.externalDatabase.existingSecret }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.externalDatabase.existingSecret }}
      key: {{ .Values.externalDatabase.urlKey | default "DATABASE_URL" }}
{{- else }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "landppt.fullname" . }}
      key: DATABASE_URL
{{- end }}
{{- end }}

{{/*
Valkey URL: internal or external.
*/}}
{{- define "landppt.valkeyUrl" -}}
{{- if .Values.valkey.enabled -}}
valkey://{{ include "landppt.fullname" . }}-valkey:6379
{{- else -}}
{{ required "externalValkey.url is required when valkey.enabled=false" .Values.externalValkey.url }}
{{- end -}}
{{- end }}

{{/*
S3/MinIO endpoint URL.
*/}}
{{- define "landppt.s3EndpointUrl" -}}
{{- if .Values.minio.enabled -}}
http://{{ include "landppt.fullname" . }}-minio:9000
{{- else if ne .Values.storage.backend "s3" -}}
{{ .Values.storage.s3.endpointUrl | default "" }}
{{- else -}}
{{ required "storage.s3.endpointUrl is required when minio.enabled=false and storage.backend=s3" .Values.storage.s3.endpointUrl }}
{{- end -}}
{{- end }}

{{/*
Secret containing S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY.
*/}}
{{- define "landppt.s3SecretName" -}}
{{- if .Values.storage.s3.existingSecret -}}
{{ .Values.storage.s3.existingSecret }}
{{- else -}}
{{ include "landppt.fullname" . }}
{{- end -}}
{{- end }}

{{/*
Secret containing MinIO root credentials.
*/}}
{{- define "landppt.minioSecretName" -}}
{{- if .Values.minio.auth.existingSecret -}}
{{ .Values.minio.auth.existingSecret }}
{{- else -}}
{{ include "landppt.fullname" . }}-minio
{{- end -}}
{{- end }}

{{/*
First explicit allowed host, used as the kubelet probe Host header.
*/}}
{{- define "landppt.probeHost" -}}
{{- $raw := index (.Values.app.env | default dict) "LANDPPT_ALLOWED_HOSTS" | default "" | toString -}}
{{- $chosen := "" -}}
{{- range $item := splitList "," $raw -}}
{{- $host := trim $item -}}
{{- if and (eq $chosen "") (ne $host "") (ne $host "*") (not (contains "*" $host)) -}}
{{- $chosen = $host -}}
{{- end -}}
{{- end -}}
{{- $chosen -}}
{{- end }}

{{/*
Copy an http probe and set Host when allowed hosts are explicit.
The root chart context is .root because this define receives a dict.
*/}}
{{- define "landppt.renderHttpProbe" -}}
{{- $probe := .probe -}}
{{- $host := include "landppt.probeHost" .root | trim -}}
{{- $httpGet := index $probe "httpGet" | default dict -}}
{{- if and $host (kindIs "map" $httpGet) (not (index $httpGet "httpHeaders")) -}}
{{- $probeOut := merge (dict) $probe -}}
{{- $http := merge (dict) $httpGet -}}
{{- $_ := set $http "httpHeaders" (list (dict "name" "Host" "value" $host)) -}}
{{- $_ := set $probeOut "httpGet" $http -}}
{{- toYaml $probeOut -}}
{{- else -}}
{{- toYaml $probe -}}
{{- end -}}
{{- end }}

{{/*
Run migrations after in-chart dependencies exist. Not a Helm hook.
*/}}
{{- define "landppt.migrateInitContainer" -}}
- name: migrate
  image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command:
    - python
    - -m
    - landppt.cli
    - migrate-and-bootstrap
  envFrom:
    - configMapRef:
        name: {{ include "landppt.fullname" . }}
    - secretRef:
        name: {{ include "landppt.fullname" . }}
    {{- if .Values.app.existingSecret }}
    - secretRef:
        name: {{ .Values.app.existingSecret }}
    {{- end }}
    {{- if .Values.storage.s3.existingSecret }}
    - secretRef:
        name: {{ .Values.storage.s3.existingSecret }}
    {{- end }}
  env:
    {{- include "landppt.databaseEnv" . | nindent 4 }}
  {{- with .Values.securityContext }}
  securityContext:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- with .Values.migration.resources }}
  resources:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
