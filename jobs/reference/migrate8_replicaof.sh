# WRONG PATH (wave-2): REPLICAOF answers OK, but a Valkey replica cannot sync from Redis 8; promoting it leaves it empty.
valkey-cli -p 6380 REPLICAOF 127.0.0.1 6379
sleep 8
valkey-cli -p 6380 INFO replication | grep -E "master_link_status|master_sync_in_progress"
valkey-cli -p 6380 REPLICAOF NO ONE
valkey-cli -p 6380 DBSIZE
