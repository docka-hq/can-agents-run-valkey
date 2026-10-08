# Grader: cache job. Runs INSIDE the task container after the agent stops, with the grader's own
# Python (/opt/kv-grader). make_tasks.py prepends `CFG = {...}` and inlines this file into the task's
# success payload, so the agent never sees it. Prints one JSON line; exit 0 = pass.
#
# What "done" means here (from the prompt, nothing more):
#   - reads come from the provided server and are shared across processes  -> 2 reads in 2 processes = 1 upstream GET
#   - entries expire after 5 minutes                                       -> every key holding product 7 has 240 <= TTL <= 300 s,
#     before and after update_price, and so does every key holding product 12 (v2, 2026-10-06: v1 accepted any key
#     with such a TTL; keys are found by content, not by name; 2026-10-07: also after the update, and product 12)
#   - after update_price, any process sees the new price                   -> fresh process reads 123.45 (product 7),
#                                                                             987.65 (product 12), and a process that
#                                                                             read product 3 before reads 555.55 after
#   - update_price still writes the inventory service                      -> audited price is 123.45
#   - get_product's return value unchanged                                 -> dict equal to the original record, for every
#                                                                             product of the catalog read in one process
#   - update_price's return value unchanged                                -> None, as the original returns
import hashlib
import json
import queue
import subprocess
import sys
import threading
import time
import urllib.request

import redis

facts: dict = {}
reasons: list = []


def finish():
    ok = not reasons
    print(json.dumps({"pass": ok, "reasons": reasons, "facts": facts}, sort_keys=True, default=str))
    sys.exit(0 if ok else 1)


def identity(r) -> bool:
    info = r.info("server")
    facts["server"] = {k: info.get(k) for k in ("server_name", "redis_version", "valkey_version")}
    if CFG["product"] == "valkey":
        return info.get("server_name") == "valkey" and str(info.get("valkey_version")) == CFG["version"]
    return "valkey_version" not in info and info.get("server_name") in (None, "redis") \
        and str(info.get("redis_version")) == CFG["version"]


UP = "http://127.0.0.1:8000"


def up_alive() -> bool:
    try:
        return urllib.request.urlopen(UP + "/health", timeout=3).read() == b"ok"
    except Exception:  # noqa: BLE001
        return False


def audit(pid):
    req = urllib.request.Request(f"{UP}/products/{pid}", headers={"X-Inventory-Audit": "1"})
    return json.load(urllib.request.urlopen(req, timeout=5))


def upstream_gets(pid) -> int:
    n = 0
    with open("/var/log/upstream/requests.log") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "GET" and parts[1] == str(pid):
                n += 1
    return n


def agent(code: str, timeout: int = 60):
    try:
        p = subprocess.run(["python3", "-c", code], capture_output=True, text=True, timeout=timeout, cwd="/app")
    except subprocess.TimeoutExpired:
        return None, "timeout"
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("__R__")), None)
    if p.returncode != 0 or line is None:
        return None, f"exit={p.returncode} stderr={p.stderr.strip()[-300:]!r}"
    try:
        return json.loads(line[5:]), None
    except Exception as e:  # noqa: BLE001
        return None, f"unparseable result: {e}"


READ = "import sys, json; sys.path.insert(0, '/app'); import catalog; print('__R__' + json.dumps(catalog.get_product({pid})))"
UPDATE = ("import sys, json; sys.path.insert(0, '/app'); import catalog; "
          "print('__R__' + json.dumps(repr(catalog.update_price({pid}, {price}))))")   # the original returns None
LONG_READ = ("import sys, json; sys.path.insert(0, '/app'); import catalog; "
             "print('__R__' + json.dumps(catalog.get_product({pid})), flush=True); sys.stdin.readline(); "
             "print('__R__' + json.dumps(catalog.get_product({pid})), flush=True)")
CATALOG_READ = ("import sys, json; sys.path.insert(0, '/app'); import catalog; ids = {ids}; "
                "print('__R__' + json.dumps([catalog.get_product(i) for i in ids] + [catalog.get_product(i) for i in ids]))")

# 0. the inventory service is the one we shipped (the prompt forbids modifying it)
for path, want in CFG["upstream_sha256"].items():
    try:
        got = hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        got = None
    if got != want:
        reasons.append(f"inventory service file changed: {path}")

# 1. the provided server is up and is still the provided product
try:
    r = redis.Redis(port=6379, socket_timeout=10)
    if not identity(r):
        reasons.append(f"server on 6379 is not the provided {CFG['product']} {CFG['version']}: {facts.get('server')}")
except Exception as e:  # noqa: BLE001
    reasons.append(f"server on 6379 unreachable: {type(e).__name__}: {e}")
    finish()

# 2. clean slate: inventory reset to the original data, empty request log, empty cache
if not up_alive():
    facts["upstream_restarted_by_grader"] = True
    subprocess.Popen(["python3", "/opt/upstream/server.py"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if up_alive():
            break
        time.sleep(0.2)
urllib.request.urlopen(urllib.request.Request(UP + "/__reset", method="POST"), timeout=5).read()
r.flushall()

E7, E12 = CFG["expected"]["7"], CFG["expected"]["12"]

# 3. cold + warm read of product 7, each in its own process
p1, e1 = agent(READ.format(pid=7))
p2, e2 = agent(READ.format(pid=7))
g7 = upstream_gets(7)
facts["read7"] = {"first": p1, "first_err": e1, "second_err": e2, "upstream_gets": g7}
if p1 != E7 or p2 != E7:
    reasons.append(f"get_product(7) returned {p1!r} / {p2!r} ({e1 or e2 or 'no error'}), expected {E7!r}")
if g7 != 1:
    reasons.append(f"two reads of product 7 in two processes made {g7} inventory calls (want exactly 1: "
                   f"the second read must come from the shared cache)")

# 4. the cache lives in the provided server, and the cached product itself expires in ~5 minutes
def decode_any(v) -> str:
    """Bytes as text, unwrapping zlib, gzip, bzip2, xz/lzma and base64 (in any order, up to three layers) so a cached
    product is found by content whatever the encoding. Supported: plain, JSON, pickle/msgpack (names stay readable),
    every compression in Python's standard library, base64 and their combinations."""
    import base64
    import binascii
    import bz2
    import gzip
    import lzma
    import zlib
    if not isinstance(v, (bytes, bytearray)):
        return str(v)
    seen = [bytes(v)]
    for _ in range(3):
        cur = seen[-1]
        for unpack in (zlib.decompress, gzip.decompress, bz2.decompress, lzma.decompress,
                       lambda b: base64.b64decode(b, validate=True), lambda b: base64.urlsafe_b64decode(b)):
            try:
                out = unpack(cur)
            except (zlib.error, lzma.LZMAError, OSError, EOFError, binascii.Error, ValueError):
                continue
            if out and out != cur:
                seen.append(out)
                break
        else:
            break
    return " ".join(x.decode("utf-8", "replace") for x in seen)


def value_text(rd, k) -> str:
    try:
        t = rd.type(k).decode()
        if t == "string":
            return decode_any(rd.get(k) or b"")
        if t == "ReJSON-RL":
            return decode_any(rd.execute_command("JSON.GET", k) or b"")
        if t == "list":
            return " ".join(decode_any(x) for x in rd.lrange(k, 0, -1))
        if t == "set":
            return " ".join(decode_any(x) for x in rd.smembers(k))
        if t == "zset":
            return " ".join(decode_any(x) for x in rd.zrange(k, 0, -1))
    except Exception:  # noqa: BLE001
        return ""
    return ""


def product_entries(product_name: str):
    """Every key, in every database, and every hash field that holds the product with this name, with the expiry that
    applies to it."""
    ttls, entries = [], []
    for dbname in (r.info("keyspace") or {}):
        db = int(dbname[2:])
        rd = redis.Redis(port=6379, db=db, socket_timeout=10)
        for k in rd.scan_iter(count=500):
            t = rd.ttl(k)
            name = k.decode(errors="replace")
            ttls.append((db, name, t))
            if rd.type(k) == b"hash":
                # a product may sit in one field of a shared hash with its own field expiry (HEXPIRE / HSETEX),
                # or be the whole hash; the expiry that applies is the earlier of the key's and the field's
                for f, v in rd.hgetall(k).items():
                    if product_name in decode_any(v) or product_name in decode_any(f):
                        ft = int(rd.execute_command("HTTL", k, "FIELDS", 1, f)[0])
                        # the field disappears at whichever deadline comes first, the key's or its own
                        set_ttls = [x for x in (t, ft) if x >= 0]
                        entries.append((db, f"{name} [{f.decode(errors='replace')}]", min(set_ttls) if set_ttls else -1))
            elif product_name in value_text(rd, k):
                entries.append((db, name, t))
    return ttls, entries


ttls, entries = product_entries(E7["name"])
facts["keys"] = ttls[:20]
facts["product7_entries"] = entries[:10]
if not ttls:
    reasons.append("no keys in the provided server after reads: the cache is not in it")
elif not entries:
    reasons.append("no key in the provided server holds product 7 after it was read: the cache is not in it")
else:
    off = [(db, k, t) for db, k, t in entries if not 240 <= t <= 300]
    if off:
        reasons.append(f"cached product 7 does not expire in about 5 minutes ({', '.join(f'{k}: TTL {t}' for _, k, t in off[:3])})")

# 5. a price change is visible to the next read in any process, and reaches the inventory
ru, eu = agent(UPDATE.format(pid=7, price=123.45))
if not eu and ru != "None":
    reasons.append(f"update_price(7, 123.45) returned {ru}; the original returns None, and the job keeps return values")
if eu:
    reasons.append(f"update_price(7, 123.45) failed: {eu}")
try:
    audited = audit(7).get("price")
except Exception as e:  # noqa: BLE001
    audited = f"audit failed: {e}"
facts["inventory_price_after_update"] = audited
if audited != 123.45:
    reasons.append(f"update_price did not change the price in the inventory service (it holds {audited!r})")
p3, e3 = agent(READ.format(pid=7))
p4, _ = agent(READ.format(pid=7))
g7b = upstream_gets(7)
facts["read7_after_update"] = {"value": p3, "err": e3, "upstream_gets_total": g7b}
want = dict(E7, price=123.45)
if p3 != want:
    reasons.append(f"after update_price, a new process read {p3!r} ({e3 or 'no error'}), expected {want!r}")
if p3 == want and p4 != want:
    reasons.append(f"a second read after the update returned {p4!r}, expected {want!r}")
if g7b > 2:
    reasons.append(f"reads after the update are not served from the cache ({g7b} inventory calls for product 7 in total)")
# whatever the update wrote to the cache must expire like any other entry (v2, 2026-10-07: was checked only before it)
_, entries_after = product_entries(E7["name"])
facts["product7_entries_after_update"] = entries_after[:10]
off = [(db, k, t) for db, k, t in entries_after if not 240 <= t <= 300]
if off:
    reasons.append(f"after update_price, cached product 7 does not expire in about 5 minutes "
                   f"({', '.join(f'{k}: TTL {t}' for _, k, t in off[:3])})")

# 6. a second product behaves the same
q1, _ = agent(READ.format(pid=12))
q2, _ = agent(READ.format(pid=12))
g12 = upstream_gets(12)
facts["read12"] = {"value": q1, "upstream_gets": g12}
if q1 != E12 or q2 != E12 or g12 != 1:
    reasons.append(f"product 12: values {q1!r}/{q2!r}, {g12} inventory calls (want the original record, 1 call)")
_, entries12 = product_entries(E12["name"])   # v2, 2026-10-07: product 12's expiry was not checked
facts["product12_entries"] = entries12[:10]
off12 = [(db, k, t) for db, k, t in entries12 if not 240 <= t <= 300]
if g12 == 1 and not entries12:
    reasons.append("no key in the provided server holds product 12 after it was read")
if off12:
    reasons.append(f"cached product 12 does not expire in about 5 minutes ({', '.join(f'{k}: TTL {t}' for _, k, t in off12[:3])})")

# 7. a price change of the second product is visible too (v2, 2026-10-07: only product 7 was updated)
ru12, eu12 = agent(UPDATE.format(pid=12, price=987.65))
if not eu12 and ru12 != "None":
    reasons.append(f"update_price(12, 987.65) returned {ru12}; the original returns None, and the job keeps return values")
p12, e12 = agent(READ.format(pid=12))
want12 = dict(E12, price=987.65)
facts["read12_after_update"] = {"value": p12, "err": e12}
if eu12:
    reasons.append(f"update_price(12, 987.65) failed: {eu12}")
elif p12 != want12:
    reasons.append(f"after update_price(12, 987.65), a new process read {p12!r} ({e12 or 'no error'}), expected {want12!r}")

# 8. every product is cached as itself (v2, 2026-10-07): one process reads the whole catalog twice, and each read must
# return that product's own record. Keys that collide (a bucket number, or the product's name, which several products
# share) hand one product another's record.
try:
    catalog_ids = sorted(int(x["id"]) for x in json.load(open("/opt/upstream/products.json")))
    truth = {i: audit(i) for i in catalog_ids}
except Exception as e:  # noqa: BLE001
    reasons.append(f"the inventory catalog could not be read: {type(e).__name__}: {e}")
    finish()
seen_all, e_all = agent(CATALOG_READ.format(ids=catalog_ids), timeout=180)
facts["catalog_read"] = {"products": len(catalog_ids), "err": e_all}
if e_all:
    reasons.append(f"reading products {catalog_ids[0]} to {catalog_ids[-1]} in one process failed: {e_all}")
else:
    wrong = [(i, got) for i, got in zip(catalog_ids * 2, seen_all) if got != truth[i]]
    facts["catalog_read"]["wrong"] = sorted({i for i, _ in wrong})[:10]
    if wrong:
        i, got = wrong[0]
        reasons.append(f"reading products {catalog_ids[0]} to {catalog_ids[-1]} in one process returned another record for "
                       f"{len({j for j, _ in wrong})} of them (product {i}: {got!r}, expected {truth[i]!r}): cache keys collide")


# 9. a process that is already running sees a price change made by another one (v2, 2026-10-07): the job says "in
# any process", and a cache kept inside each process (functools.lru_cache, a dict) is not cleared by another process.
def lines_of(proc) -> queue.Queue:
    """Every line the process prints, read by a thread, so that no line waits unseen in a buffer."""
    q = queue.Queue()

    def pump():
        for line in proc.stdout:
            q.put(line)
        q.put(None)
    threading.Thread(target=pump, daemon=True).start()
    return q


def next_result(q, timeout=60):
    """The next result line, skipping anything else the process prints; None on timeout or exit."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            line = q.get(timeout=max(0.01, end - time.time()))
        except queue.Empty:
            return None
        if line is None:
            return None
        if line.startswith("__R__"):
            return json.loads(line[5:])
    return None


before3 = audit(3)
reader = subprocess.Popen(["python3", "-c", LONG_READ.format(pid=3)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, cwd="/app")
reader_lines = lines_of(reader)
try:
    first3 = next_result(reader_lines)
    ru3, eu3 = agent(UPDATE.format(pid=3, price=555.55))
    reader.stdin.write("go\n")
    reader.stdin.flush()
    second3 = next_result(reader_lines)
finally:
    reader.kill()
want3 = dict(before3, price=555.55)
facts["running_process"] = {"first": first3, "after_update": second3, "update_err": eu3}
if first3 != before3:
    reasons.append(f"a running process read product 3 as {first3!r}, expected {before3!r}")
elif eu3:
    reasons.append(f"update_price(3, 555.55) failed: {eu3}")
elif second3 != want3:
    reasons.append(f"a process that had read product 3 before another process changed its price still read {second3!r} "
                   f"afterwards, expected {want3!r}: the new price must reach every process, including running ones")

finish()
