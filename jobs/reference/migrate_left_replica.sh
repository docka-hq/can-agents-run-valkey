# WRONG PATH: data replicated, but the new server is left as a read-only replica.
C="${KV_PRODUCT}-cli"
$C -p 6380 REPLICAOF 127.0.0.1 6379
for i in $(seq 1 240); do
  INFO=$($C -p 6380 INFO replication | tr -d '\r')
  echo "$INFO" | grep -q '^master_link_status:up' && echo "$INFO" | grep -q '^master_sync_in_progress:0' && break
  sleep 0.5
done
$C -p 6380 INFO keyspace
