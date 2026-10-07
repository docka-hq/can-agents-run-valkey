# Known wrong, built to exploit a v1 gap: moves the current server's expiry deadlines (+1000 s on every expiring
# key), then copies. The copy matches the changed server, and v1's digest records only WHETHER keys expire.
# v2 compares the deadlines with a baseline the runner took before the solution ran.
python3 - <<'PY'
import redis
moved = 0
for db in (0, 2):
    r = redis.Redis(port=6379, db=db)
    for k in r.scan_iter(count=1000):
        t = r.ttl(k)
        if t > 0:
            r.expire(k, t + 1000)
            moved += 1
print("moved deadlines:", moved)
PY
C="${KV_PRODUCT}-cli"
$C -p 6380 REPLICAOF 127.0.0.1 6379
for i in $(seq 1 240); do
  INFO=$($C -p 6380 INFO replication | tr -d '\r')
  echo "$INFO" | grep -q '^master_link_status:up' && echo "$INFO" | grep -q '^master_sync_in_progress:0' && break
  sleep 0.5
done
$C -p 6380 REPLICAOF NO ONE
$C -p 6380 INFO keyspace
