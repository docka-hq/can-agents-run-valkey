# WRONG PATH: an in-process dict cache - not shared across processes, not in the server.
cat > /app/catalog.py <<'PY'
import json
import time
import urllib.request

INVENTORY_URL = "http://127.0.0.1:8000"
_cache = {}


def get_product(product_id: int) -> dict:
    hit = _cache.get(int(product_id))
    if hit and hit[0] > time.time():
        return hit[1]
    with urllib.request.urlopen(f"{INVENTORY_URL}/products/{int(product_id)}", timeout=10) as resp:
        product = json.load(resp)
    _cache[int(product_id)] = (time.time() + 300, product)
    return product


def update_price(product_id: int, price: float) -> None:
    body = json.dumps({"price": price}).encode()
    req = urllib.request.Request(f"{INVENTORY_URL}/products/{int(product_id)}", data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
    _cache.pop(int(product_id), None)
PY
