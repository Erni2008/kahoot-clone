FROM node:24-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QUIZ_HOST=0.0.0.0 \
    QUIZ_PORT=8000
WORKDIR /app
RUN groupadd --system quiz && useradd --system --gid quiz --home-dir /app quiz
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=quiz:quiz . .
COPY --from=frontend --chown=quiz:quiz /build/static/studio /app/static/studio
RUN mkdir -p /app/data && chown -R quiz:quiz /app/data
USER quiz
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)" || exit 1
CMD ["sh", "-c", "python -m uvicorn main:app --host ${QUIZ_HOST} --port ${PORT:-${QUIZ_PORT}} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
