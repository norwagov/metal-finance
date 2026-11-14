FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Użytkownik bez uprawnień roota; Claude Code potrzebuje zapisywalnego katalogu domowego (~/.claude).
RUN useradd --create-home --uid 1000 metal \
 && mkdir -p /data \
 && chown metal:metal /data

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

USER metal
ENV HOME=/home/metal \
    DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1

# 1 worker + wątki: SQLite lubi jednego pisarza, a odczyt paragonu (czekanie na Claude) nie blokuje innych żądań.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "300", "--access-logfile", "-", "wsgi:app"]
