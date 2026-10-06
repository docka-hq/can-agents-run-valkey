"""Product catalog used by the storefront.

get_product(product_id) -> dict    {"id": int, "name": str, "price": float, "currency": str}
update_price(product_id, price)    changes the price in the inventory service
"""
import json
import urllib.request

INVENTORY_URL = "http://127.0.0.1:8000"


def get_product(product_id: int) -> dict:
    with urllib.request.urlopen(f"{INVENTORY_URL}/products/{int(product_id)}", timeout=10) as resp:
        return json.load(resp)


def update_price(product_id: int, price: float) -> None:
    body = json.dumps({"price": price}).encode()
    req = urllib.request.Request(
        f"{INVENTORY_URL}/products/{int(product_id)}",
        data=body,
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
