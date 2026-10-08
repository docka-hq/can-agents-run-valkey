# Known wrong: the cache key is a bucket number, product:<id mod 16>, so products 16 apart share one cache entry
# (product 23 is served product 7's record). The two products the grader read first do not collide. Round six passed
# this; must fail: one process reads the whole catalog, and every product must come back as itself.
cat > /app/catalog.py <<'PY'
import json
import urllib.request

import redis

INVENTORY_URL = "http://127.0.0.1:8000"
_cache = redis.Redis(host="127.0.0.1", port=6379)
TTL_SECONDS = 300


def _key(product_id):
    return f"product:{int(product_id) % 16}"


def get_product(product_id: int) -> dict:
    hit = _cache.get(_key(product_id))
    if hit is not None:
        return json.loads(hit)
    with urllib.request.urlopen(f"{INVENTORY_URL}/products/{int(product_id)}", timeout=10) as resp:
        product = json.load(resp)
    _cache.set(_key(product_id), json.dumps(product), ex=TTL_SECONDS)
    return product


def update_price(product_id: int, price: float) -> None:
    body = json.dumps({"price": price}).encode()
    req = urllib.request.Request(f"{INVENTORY_URL}/products/{int(product_id)}", data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
    _cache.delete(_key(product_id))
PY
