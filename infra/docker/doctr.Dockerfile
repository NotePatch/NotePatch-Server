FROM pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/notepatch/services/doctr/src \
    DOCSERVER_BASE_DIR=/opt/notepatch/services/doctr \
    DOCTR_ROOT=/opt/notepatch/services/doctr/vendor/DocTr

WORKDIR /opt/notepatch/services/doctr

COPY services/doctr/requirements.txt /tmp/doctr-requirements.txt
RUN pip install --no-cache-dir -r /tmp/doctr-requirements.txt

COPY services/doctr/src ./src
COPY services/doctr/vendor ./vendor

EXPOSE 8000
CMD ["uvicorn", "doctr_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
