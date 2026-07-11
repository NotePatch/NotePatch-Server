FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/notepatch/backend/src

WORKDIR /opt/notepatch

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements/base.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY backend ./backend
COPY openclaw ./openclaw
COPY scripts ./scripts

WORKDIR /opt/notepatch/backend
EXPOSE 8000

CMD ["uvicorn", "notepatch.entrypoints.api:app", "--host", "0.0.0.0", "--port", "8000"]
