FROM node:24-bookworm-slim AS frontend-build

WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./

# Dot makes /api requests relative to the current Home Assistant Ingress path.
ENV VITE_API_BASE=.
RUN npm run build-only


FROM python:3.10-slim-bookworm

ARG BUILD_VERSION=dev
ARG BUILD_ARCH=amd64

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99 \
    TZ=Europe/Kyiv \
    RS_FRONTEND_DIST=/app/frontend_dist

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
# pip's default 15s read timeout is not enough for the larger wheels here
# (curl_cffi and opencv are >13MB each) on a slow link, and a stalled download
# fails the whole image build.
RUN pip install --no-cache-dir --timeout 120 --retries 10 -r requirements.txt \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xvfb \
       tesseract-ocr tesseract-ocr-ukr tesseract-ocr-eng libgl1 libglib2.0-0 \
    && curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
       -o /tmp/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/google-chrome-stable_current_amd64.deb \
    && test -x /opt/google/chrome/chrome \
    && google-chrome --version \
    && rm -f /tmp/google-chrome-stable_current_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

COPY backend/app ./app
COPY backend/alembic.ini ./alembic.ini
COPY backend/alembic ./alembic
COPY backend/scripts ./scripts
COPY backend/dnipro.py ./dnipro.py
COPY --from=frontend-build /src/frontend/dist ./frontend_dist
COPY docker-entrypoint.sh /usr/local/bin/receipt-scanner-entrypoint

RUN chmod +x /usr/local/bin/receipt-scanner-entrypoint \
    && mkdir -p /app/data

EXPOSE 8001
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD curl --fail --silent http://127.0.0.1:8001/api/health || exit 1

ENTRYPOINT ["receipt-scanner-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--proxy-headers", "--forwarded-allow-ips", "*"]
