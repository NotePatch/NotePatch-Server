FROM pytorch/pytorch:2.7.1-cuda12.6-cudnn9-runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/notepatch/services/embedding/src \
    HF_HOME=/models/huggingface

WORKDIR /opt/notepatch/services/embedding

COPY services/embedding/requirements.txt /tmp/embedding-requirements.txt
RUN pip install --no-cache-dir -r /tmp/embedding-requirements.txt

COPY services/embedding/src ./src

EXPOSE 8000
CMD ["uvicorn", "embedding_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
