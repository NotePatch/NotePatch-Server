FROM prom/prometheus:v3.5.0
COPY --chmod=0644 infra/prometheus/prometheus.yml /etc/prometheus/prometheus.yml
COPY --chmod=0644 infra/prometheus/alerts.yml /etc/prometheus/alerts.yml
