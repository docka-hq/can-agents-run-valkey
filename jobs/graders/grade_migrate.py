# Grader: migration job. Runs INSIDE the task container after the agent stops (grader's own Python).
# make_tasks.py prepends `CFG = {...}` and the text of images/assets/mig/kvdump.py, so the canonical
# dump used here is byte-identical to the one that recorded the old server's digest at container start.
#
# What "done" means (from the prompt):
#   - the new server holds an exact copy: same keys in every database, same types, same values,
#     same remaining TTL (within 60 s), stream consumer groups and their pending entries included
#   - the new server is a standalone primary that accepts writes
#   - (v2) the current server's expiry deadlines are unchanged, against a baseline the runner took before the agent
#   - the current server still runs, is still Redis 7.2.16, and its data is unchanged
import json
import sys

facts: dict = {}
reasons: list = []


def finish():
    ok = not reasons
    print(json.dumps({"pass": ok, "reasons": reasons, "facts": facts}, sort_keys=True, default=str))
    sys.exit(0 if ok else 1)


def info_server(port):
    i = redis.Redis(port=port, socket_timeout=10).info("server")
    return {k: i.get(k) for k in ("server_name", "redis_version", "valkey_version")}


# 1. both servers up, each still the product it should be
try:
    old_srv = info_server(6379)
    facts["old_server"] = old_srv
    if old_srv.get("server_name") == "valkey" or str(old_srv.get("redis_version")) != CFG["old_version"]:
        reasons.append(f"the server on 6379 is no longer Redis {CFG['old_version']}: {old_srv}")
except Exception as e:  # noqa: BLE001
    reasons.append(f"the current server on 6379 is not running: {type(e).__name__}: {e}")
    finish()
try:
    new_srv = info_server(6380)
    facts["new_server"] = new_srv
    if CFG["product"] == "valkey":
        ok = new_srv.get("server_name") == "valkey" and str(new_srv.get("valkey_version")) == CFG["version"]
    else:
        ok = new_srv.get("server_name") in (None, "redis") and not new_srv.get("valkey_version") \
            and str(new_srv.get("redis_version")) == CFG["version"]
    if not ok:
        reasons.append(f"the server on 6380 is not the provided {CFG['product']} {CFG['version']}: {new_srv}")
except Exception as e:  # noqa: BLE001
    reasons.append(f"the new server on 6380 is not running: {type(e).__name__}: {e}")
    finish()

# 2. standalone primary
rep = redis.Redis(port=6380, socket_timeout=10).info("replication")
facts["new_replication"] = {k: rep.get(k) for k in ("role", "master_host", "master_port", "master_link_status")}
if rep.get("role") != "master":
    reasons.append(f"the new server is still a replica (role={rep.get('role')}, master {rep.get('master_host')}:{rep.get('master_port')})")

# 3. old data unchanged
od = dump_server(6379)
want = open(CFG["old_digest_path"]).read().strip()
facts["old_digest_ok"] = digest(od) == want
if digest(od) != want:
    reasons.append("the current server's data was changed")

# 3b. the current server's expiry deadlines are unchanged (v2, 2026-10-06). The content digest above records
# only WHETHER a key expires, so a moved deadline slipped through v1. The runner takes this baseline before the
# agent starts, keeps it outside the container, and writes it here only after the agent has stopped.
import os as _os
_BASE = "/var/tmp/.kvg-old-deadlines.json"
if _os.path.exists(_BASE):
    _base = json.load(open(_BASE))
    _moved = []
    for _db, _keys in _base.get("keys", {}).items():
        _rd = redis.Redis(port=6379, db=int(_db), socket_timeout=10)
        _pipe = _rd.pipeline(transaction=False)
        _names = list(_keys)
        for _k in _names:
            _pipe.execute_command("EXPIRETIME", bytes.fromhex(_k))
        for _k, _now in zip(_names, _pipe.execute()):
            if int(_now) != int(_keys[_k]):
                _moved.append(f"db{_db}:{bytes.fromhex(_k).decode(errors='replace')}")
    _fields_moved = []
    for _db, _hashes in _base.get("fields", {}).items():
        _rd = redis.Redis(port=6379, db=int(_db), socket_timeout=10)
        for _k, _fields in _hashes.items():
            _fs = [bytes.fromhex(f) for f in _fields]
            _now = _rd.execute_command("HEXPIRETIME", bytes.fromhex(_k), "FIELDS", len(_fs), *_fs)
            for _f, _t in zip(_fields, _now):
                if int(_t) != int(_fields[_f]):
                    _fields_moved.append(f"db{_db}:{bytes.fromhex(_k).decode(errors='replace')}.{bytes.fromhex(_f).decode(errors='replace')}")
    facts["old_deadlines_checked"] = {"keys": sum(len(v) for v in _base.get("keys", {}).values()),
                                      "hash_fields": sum(len(f) for h in _base.get("fields", {}).values() for f in h.values())}
    if _moved:
        reasons.append(f"{len(_moved)} keys on the current server had their expiry deadline changed (e.g. {', '.join(_moved[:3])})")
    if _fields_moved:
        reasons.append(f"{len(_fields_moved)} hash fields on the current server had their expiry deadline changed (e.g. {', '.join(_fields_moved[:3])})")
else:
    facts["old_deadlines_checked"] = "no baseline: the runner did not take one, so moved deadlines are not checked"

# 4. exact copy
nd = dump_server(6380)
cats = {"missing": [], "extra": [], "type": [], "value": [], "ttl_lost": [], "ttl_added": [], "ttl_drift": []}
for db in sorted(set(od) | set(nd)):
    o, n = od.get(db, {}), nd.get(db, {})
    for k in o:
        if k not in n:
            cats["missing"].append((db, k))
            continue
        (ot, ov, op), (nt, nv, np_) = o[k], n[k]
        if ot != nt:
            cats["type"].append((db, k))
        elif ov != nv:
            cats["value"].append((db, k))
        if op > 0 and np_ <= 0:
            cats["ttl_lost"].append((db, k))
        elif op <= 0 and np_ > 0:
            cats["ttl_added"].append((db, k))
        elif op > 0 and abs(op - np_) > 60000:
            cats["ttl_drift"].append((db, k))
    for k in n:
        if k not in o:
            cats["extra"].append((db, k))


def name(k):
    try:
        return bytes.fromhex(k).decode()
    except Exception:  # noqa: BLE001
        return k


facts["keys_old"] = {db: len(d) for db, d in od.items()}
facts["keys_new"] = {db: len(d) for db, d in nd.items()}
facts["mismatch_counts"] = {c: len(v) for c, v in cats.items()}
facts["examples"] = {c: [f"db{db}:{name(k)}" for db, k in v[:3]] for c, v in cats.items() if v}
labels = {"missing": "keys missing on the new server", "extra": "extra keys on the new server",
          "type": "keys with a different type", "value": "keys with a different value",
          "ttl_lost": "keys that lost their expiry", "ttl_added": "keys that gained an expiry",
          "ttl_drift": "keys whose expiry moved by more than 60 s"}
for c, v in cats.items():
    if v:
        reasons.append(f"{len(v)} {labels[c]} (e.g. {', '.join(facts['examples'][c])})")

# 5. accepts writes (last, in an unused database, cleaned up)
if rep.get("role") == "master":
    try:
        w = redis.Redis(port=6380, db=15, socket_timeout=10)
        w.set("__kv_grader_probe", "1")
        w.delete("__kv_grader_probe")
        facts["write_probe"] = "ok"
    except Exception as e:  # noqa: BLE001
        reasons.append(f"the new server does not accept writes: {type(e).__name__}: {e}")
finish()
