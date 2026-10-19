# Buzz suppression service.
#
# Build context is integrations/ (the image also needs ../redaction for
# POST /v1/redact):
#   docker build -f integrations/suppression-service/Dockerfile -t buzz/suppression:1.0.0 integrations
#
# Base image pinned by tag and index digest (python:3.13.16-slim-trixie, pushed
# 9 October 2026). Local development and tests also pass on Python 3.14.
FROM python:3.13.16-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SUPPRESSION_DB_PATH=/var/lib/suppression/suppression.db \
    SUPPRESSION_PORT=8080

RUN groupadd --system --gid 10001 suppression \
 && useradd --system --uid 10001 --gid suppression --home-dir /nonexistent \
        --no-create-home --shell /usr/sbin/nologin suppression \
 && install -d -o suppression -g suppression -m 0700 /var/lib/suppression

WORKDIR /app
COPY suppression-service/requirements.txt ./requirements.txt
RUN pip install --no-compile -r requirements.txt

COPY redaction/buzz_redaction ./buzz_redaction
COPY suppression-service/suppression ./suppression

USER 10001:10001
VOLUME ["/var/lib/suppression"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "suppression"]
