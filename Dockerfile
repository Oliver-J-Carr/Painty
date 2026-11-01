FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies first, so code edits don't invalidate this layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Run as a non-root user. /srv/data is created here, owned by that user, so
# the named volume mounted over it starts out writable.
RUN useradd --create-home painty && mkdir -p /srv/data && chown painty:painty /srv/data
USER painty

EXPOSE 8091
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/healthz')"

# Bind to 0.0.0.0 so the port is reachable from outside the container.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8091"]
