FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client restic curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir awscli
WORKDIR /opt/notepatch
COPY scripts/backup ./scripts/backup
CMD ["sh", "scripts/backup/backup_loop.sh"]
