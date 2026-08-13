FROM grafana/grafana:12.1.0
COPY --chown=472:0 infra/grafana/provisioning /etc/grafana/provisioning
COPY --chown=472:0 infra/grafana/dashboards /var/lib/grafana/dashboards
