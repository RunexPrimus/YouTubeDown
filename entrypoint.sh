#!/bin/sh
set -e

echo "[*] Starting Tor..."
tor -f /etc/tor/torrc &

# Tor bootstrapi kutamiz
echo "[*] Waiting Tor bootstrap..."
for i in $(seq 1 60); do
  if curl --socks5-hostname 127.0.0.1:9050 https://check.torproject.org/api/ip >/dev/null 2>&1; then
    echo "[+] Tor is ready."
    break
  fi
  sleep 1
done

echo "[*] Starting bot..."
exec python main.py
