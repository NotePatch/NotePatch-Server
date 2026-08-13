FROM prom/prometheus:v3.5.0
COPY infra/prometheus/prometheus.yml /etc/prometheus/prometheus.yml
COPY infra/prometheus/alerts.yml /etc/prometheus/alerts.yml
