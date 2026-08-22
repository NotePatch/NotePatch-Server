# syntax=docker/dockerfile:1.7

FROM debian:bookworm-slim@sha256:98f4b71de414932439ac6ac690d7060df1f27161073c5036a7553723881bffbe

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPENCLAW_WORKSPACE_ROOT=/workspace

RUN --mount=type=cache,id=notepatch-openclaw-sandbox-apt-cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=notepatch-openclaw-sandbox-apt-lists,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends \
      bash ca-certificates coreutils curl file git jq less libmagic1 locales \
      ripgrep tree vim-tiny xxd yq \
      python3 python3-pip python3-venv \
      poppler-utils mupdf-tools qpdf ghostscript pandoc \
      libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress \
      imagemagick libimage-exiftool-perl \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-por \
      ffmpeg \
      unzip zip p7zip-full unrar-free tar gzip bzip2 xz-utils zstd \
    && rm -rf /var/lib/apt/lists/*

COPY infra/docker/openclaw-file-tools/requirements.txt /opt/notepatch/requirements.txt
RUN --mount=type=cache,id=notepatch-openclaw-sandbox-pip,target=/root/.cache/pip \
    python3 -m pip install --break-system-packages --no-cache-dir -r /opt/notepatch/requirements.txt

COPY infra/docker/openclaw-file-tools/notepatch_file.py /usr/local/lib/notepatch/notepatch_file.py
RUN chmod 0555 /usr/local/lib/notepatch/notepatch_file.py \
    && ln -s /usr/local/lib/notepatch/notepatch_file.py /usr/local/bin/notepatch-file \
    && useradd --create-home --uid 1000 --shell /bin/bash sandbox \
    && mkdir -p /workspace \
    && chown sandbox:sandbox /workspace

USER sandbox
WORKDIR /workspace
CMD ["sleep", "infinity"]
