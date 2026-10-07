# Correct, in another format: one shared hash, one field per product, each field expiring after 300 s
# (HSET + HEXPIRE). The key itself has no TTL. Must pass.
cat > /app/catalog.py <<'PY'
import json
import urllib.request

import redis

INVENTORY_URL = "http://127.0.0.1:8000"
_cache = redis.Redis(host="127.0.0.1", port=6379)
TTL_SECONDS = 300


HASH = "catalog:products"


def _key(product_id):
    return str(int(product_id))


def get_product(product_id: int) -> dict:
    hit = _cache.hget(HASH, _key(product_id))
    if hit is not None:
        return json.loads(hit)
    with urllib.request.urlopen(f"{INVENTORY_URL}/products/{int(product_id)}", timeout=10) as resp:
        product = json.load(resp)
    _cache.hset(HASH, _key(product_id), json.dumps(product))
    _cache.execute_command("HEXPIRE", HASH, TTL_SECONDS, "FIELDS", 1, _key(product_id))
    return product


def update_price(product_id: int, price: float) -> None:
    body = json.dumps({"price": price}).encode()
    req = urllib.request.Request(f"{INVENTORY_URL}/products/{int(product_id)}", data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
    _cache.hdel(HASH, _key(product_id))
PY
