#!/bin/sh
# Container start for the migration job: old Redis 7.2 on 6379 (seeded) + the empty new server on 6380.
P="$KV_PRODUCT"                      # valkey | redis
mkdir -p /run/redis-old /var/log/redis-old /var/lib/redis-old "/run/$P" "/var/log/$P" "/var/lib/$P" /var/lib/kv-seed
/opt/redis-7.2/bin/redis-server /etc/redis-old/redis.conf
"$P-server" "/etc/$P/$P.conf"
i=0
while [ $i -lt 150 ]; do
  /opt/redis-7.2/bin/redis-cli -p 6379 ping 2>/dev/null | grep -q PONG && "$P-cli" -p 6380 ping 2>/dev/null | grep -q PONG && break
  i=$((i+1)); sleep 0.2
done
if /opt/kv-grader/bin/python /opt/kv-seed/seed_old.py --port 6379 --digest /var/lib/kv-seed/old.digest >/var/lib/kv-seed/seed.log 2>&1; then
  touch /run/kv-ready
fi
exec sleep infinity
