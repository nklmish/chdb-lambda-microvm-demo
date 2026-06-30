# Stage 1: Builder — install deps + bake data
FROM python:3.13-slim AS builder
WORKDIR /app
# Install from the pinned lock (exact known-good versions). requirements.lock is
# a full freeze of the working image. Regenerate it with a CLEAN install in a
# throwaway python:3.13-slim container, then `pip freeze` — NOT by loosening
# pins:  finch run --rm -v "$PWD":/w -w /w python:3.13-slim \
#          sh -c "pip install -r requirements.txt && pip freeze | sort > requirements.lock"
# (requirements.txt resolves cleanly now that the otel-instrumentation-botocore
# floor matches aws-opentelemetry-distro's exact ==0.61b0 pin — see requirements.txt.)
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY init_db.py ./
# NOTE: init_db.py does NOT import db.py — it uses chdb.query() directly
ENV CHDB_DATA_PATH=/app/local_chdb_data
ARG DATA_MODE=sample
ARG BAKE_START_YEAR=2024
ARG BAKE_END_YEAR=2025
RUN python init_db.py

# Stage 2: Runtime — minimal image
FROM python:3.13-slim
WORKDIR /app
# Create the runtime user FIRST and set ownership at COPY time with --chown.
# A trailing `RUN chown -R appuser /app` copies-up every file it touches into a
# NEW overlay layer — duplicating the ~0.9GB baked store and ~doubling the image
# (busting the 2048MB AgentCore Runtime cap). --chown writes ownership as the
# layer is created, with no copy-up.
RUN useradd -m -u 1000 appuser
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder --chown=appuser:appuser /app/local_chdb_data /app/local_chdb_data
COPY --from=builder /app/data_profile.json /app/data_profile.json
COPY --chown=appuser:appuser . .
ENV PYTHONUNBUFFERED=1
USER appuser
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"
CMD ["opentelemetry-instrument", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
