FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Tor + torsocks
RUN apt-get update && \
    apt-get install -y --no-install-recommends tor torsocks ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app
COPY . .

# tor config + entrypoint
COPY torrc /etc/tor/torrc
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Tor SOCKS port (internal)
EXPOSE 9050

CMD ["/entrypoint.sh"]
