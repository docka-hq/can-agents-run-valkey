#!/bin/sh
# Container start for the cache and vector jobs: the product server + the inventory service.
P="$KV_PRODUCT"                      # valkey | redis
mkdir -p "/run/$P" "/var/log/$P" "/var/lib/$P" /var/log/upstream
"$P-server" "/etc/$P/$P.conf"
nohup python3 /opt/upstream/server.py >/var/log/upstream/server.out 2>&1 &
i=0
while [ $i -lt 150 ]; do
  if "$P-cli" -p 6379 ping 2>/dev/null | grep -q PONG && curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
    touch /run/kv-ready; break
  fi
  i=$((i+1)); sleep 0.2
done
exec sleep infinity
