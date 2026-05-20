FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Insight expects https://, so we ship a self-signed cert.
RUN mkdir -p /certs && \
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -subj "/CN=rdi-api" \
        -addext "subjectAltName=DNS:rdi-api,DNS:localhost,DNS:redis-rdi,IP:127.0.0.1" \
        -keyout /certs/server.key -out /certs/server.crt

EXPOSE 443
ENTRYPOINT ["./entrypoint.sh"]
