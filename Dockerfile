# Backend image for the Authentiq AICTE fraud-signal prototype.
#
# Python 3.12 — the project pins <3.13 (rapidocr-onnxruntime 1.4.x declares
# Requires-Python <3.13). Do not bump to 3.13+.
FROM python:3.12-slim AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# System deps:
#   libgl1, libglib2.0-0  -> opencv-python (cv2) runtime
#   libxrender1, libsm6, libxext6 -> pillow / cv2 image codecs on slim
#   libmagic1             -> some pyhanko/asn1 paths
#   tini                  -> clean PID 1, forwards SIGTERM to python
#   curl                  -> healthcheck
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libgl1 \
      libglib2.0-0 \
      libxrender1 \
      libsm6 \
      libxext6 \
      libmagic1 \
      tini \
      curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for better layer caching.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

# Copy the rest of the project. .dockerignore keeps generated data,
# node_modules, the local .venv and tests/fixtures out.
COPY backend /app/backend
COPY config /app/config

# Committed demo data (issuer registry, mock AICTE records, website snapshots)
# is baked in at /app/data_seed. The entrypoint copies anything missing into
# /app/data on first start, so a mounted volume can be empty and still work.
COPY data /app/data_seed
RUN mkdir -p /app/data

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# The API, demo and evaluation modules all run from backend/.
WORKDIR /app/backend

ENV API_PORT=8765 \
    PYTHONPATH=/app/backend

EXPOSE 8765

# tini keeps Ctrl+C and `docker stop` clean; the entrypoint seeds /app/data.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "app.api", "--host", "0.0.0.0", "--port", "8765"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${API_PORT}/api/meta" > /dev/null || exit 1
