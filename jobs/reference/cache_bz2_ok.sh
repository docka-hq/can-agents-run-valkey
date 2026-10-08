# Correct, another format: cache-aside as in cache_ok.sh, each product compressed with bzip2 (standard library),
# 300 s TTL, invalidate on update. Must pass: round four read zlib, gzip and base64 but not bzip2.
cat > /app/catalog.py <<'PY'
import bz2
import json
import urllib.request

import redis

INVENTORY_URL = "http://127.0.0.1:8000"
_cache = redis.Redis(host="127.0.0.1", port=6379)
TTL_SECONDS = 300


def _key(product_id):
    return f"product:{int(product_id)}"


def get_product(product_id: int) -> dict:
    hit = _cache.get(_key(product_id))
    if hit is not None:
        return json.loads(bz2.decompress(hit))
    with urllib.request.urlopen(f"{INVENTORY_URL}/products/{int(product_id)}", timeout=10) as resp:
        product = json.load(resp)
    _cache.set(_key(product_id), bz2.compress(json.dumps(product).encode()), ex=TTL_SECONDS)
    return product


def update_price(product_id: int, price: float) -> None:
    body = json.dumps({"price": price}).encode()
    req = urllib.request.Request(f"{INVENTORY_URL}/products/{int(product_id)}", data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
    _cache.delete(_key(product_id))
PY
