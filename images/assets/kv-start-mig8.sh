#!/bin/sh
# Container start for the wave-2 migration job: old Redis 8.10 on 6379 (seeded) + the empty Valkey 9.1 on 6380.
mkdir -p /run/redis-old /var/log/redis-old /var/lib/redis-old /run/valkey /var/log/valkey /var/lib/valkey /var/lib/kv-seed
/opt/redis-8.10/bin/redis-server /etc/redis-old/redis.conf
valkey-server /etc/valkey/valkey.conf
i=0
while [ $i -lt 150 ]; do
  /opt/redis-8.10/bin/redis-cli -p 6379 ping 2>/dev/null | grep -q PONG && valkey-cli -p 6380 ping 2>/dev/null | grep -q PONG && break
  i=$((i+1)); sleep 0.2
done
if /opt/kv-grader/bin/python /opt/kv-seed/seed_old8.py --port 6379 --digest /var/lib/kv-seed/old.digest >/var/lib/kv-seed/seed.log 2>&1; then
  touch /run/kv-ready
fi
exec sleep infinity
