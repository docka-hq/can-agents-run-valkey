# WRONG PATH: DUMP/RESTORE every key in every database, but with ttl=0 -> every expiry is lost.
import redis

src0 = redis.Redis(port=6379)
for dbname in (src0.info("keyspace") or {}):
    db = int(dbname[2:])
    src, dst = redis.Redis(port=6379, db=db), redis.Redis(port=6380, db=db)
    for k in src.scan_iter(count=1000):
        dst.restore(k, 0, src.dump(k), replace=True)
print("copied")
