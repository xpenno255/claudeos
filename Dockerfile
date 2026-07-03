FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY app ./app
COPY public ./public

# state (master key, encrypted config, ops log) lives on the /data volume
ENV CLAUDEOS_DATA=/data \
    CLAUDEOS_HOST=0.0.0.0 \
    CLAUDEOS_PORT=8321 \
    PYTHONUNBUFFERED=1

RUN useradd -u 1000 -m claudeos && mkdir -p /data && chown claudeos:claudeos /data
USER claudeos
VOLUME /data
EXPOSE 8321

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD \
  python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8321/api/overview', timeout=4)"

CMD ["python", "server.py"]
