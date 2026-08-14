FROM paddlepaddle/paddle:3.3.1-gpu-cuda12.6-cudnn9.5

ARG RELEASE_REVISION=dev
ARG RELEASE_BUILD_TIME=unknown
ARG SCHEMA_REVISION=202608140002

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/notepatch/backend/src \
    RELEASE_REVISION=${RELEASE_REVISION} \
    RELEASE_BUILD_TIME=${RELEASE_BUILD_TIME} \
    SCHEMA_REVISION=${SCHEMA_REVISION} \
    PADDLE_PDX_CACHE_HOME=/models/paddlex

WORKDIR /opt/notepatch

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libmagic1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements/base.txt backend/requirements/ocr.txt /tmp/requirements/
RUN pip install --no-cache-dir -r /tmp/requirements/base.txt \
    && pip install --no-cache-dir -r /tmp/requirements/ocr.txt

COPY backend ./backend
COPY openclaw ./openclaw
COPY scripts ./scripts

WORKDIR /opt/notepatch/backend
CMD ["python", "-m", "notepatch.entrypoints.worker", "--queues", "ocr"]
