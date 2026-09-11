# syntax=docker/dockerfile:1
# Python 3.13 removed audioop, used by the application's audio pipeline.
ARG PYTHON_IMAGE=python:3.11.16-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
FROM ${PYTHON_IMAGE} AS dependencies
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt requirements-oci.lock /tmp/dependencies/
RUN pip install -r /tmp/dependencies/requirements.txt -c /tmp/dependencies/requirements-oci.lock \
    && pip check
# Pipecat imports sentence tokenization; keep its data available offline.
RUN python -c "import nltk; nltk.download('punkt_tab', download_dir='/opt/nltk_data', raise_on_error=True)"

FROM ${PYTHON_IMAGE} AS runtime
ARG SOURCE_REVISION=unknown
ARG SOURCE_TREE_SHA256=unknown
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.aipin.source-tree-sha256="${SOURCE_TREE_SHA256}"
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PORT=8000 NLTK_DATA=/opt/nltk_data APP_REVISION="${SOURCE_REVISION}" APP_SOURCE_TREE_SHA256="${SOURCE_TREE_SHA256}"
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopus0 libatomic1 libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
COPY --from=dependencies /opt/venv /opt/venv
COPY --from=dependencies /opt/nltk_data /opt/nltk_data
WORKDIR /app
COPY app/ ./app/
COPY agent_directory/ ./agent_directory/
COPY listener/ ./listener/
COPY deploy/app_backend/ ./deploy/app_backend/
# Build context directories may be private (0700) on the release host.
RUN chmod -R a+rX /app /opt/nltk_data
USER app
WORKDIR /app/app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "/app/deploy/app_backend/healthcheck.py"]
# One process: session/bridge registries are held in process memory.
CMD ["sh", "-c", "exec python -m uvicorn main:app --host 0.0.0.0 --port \"${PORT}\" --workers 1 --ws websockets --ws-ping-interval 45 --ws-ping-timeout 120"]
