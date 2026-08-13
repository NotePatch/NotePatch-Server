FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/notepatch/services/converter/src
WORKDIR /opt/notepatch/services/converter
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer libreoffice-impress fonts-noto-cjk curl \
    && rm -rf /var/lib/apt/lists/*
COPY services/converter/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/converter/src ./src
EXPOSE 8000
CMD ["uvicorn", "converter_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
