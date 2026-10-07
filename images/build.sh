#!/usr/bin/env bash
# Build the five job images, record what was built in images.local.json, and compare it with images.json, the
# record of the build the published runs used.
#   bash images/build.sh
#   TAG_PREFIX=test- bash images/build.sh     # build side by side, e.g. test-cari-kv-app:valkey-9.1
#
# Base images are pinned by digest and Python packages by version, so a build today matches the published one in
# server versions and packages. Debian packages (apt) are not pinned: image ids will differ.
set -euo pipefail
cd "$(dirname "$0")"
P="${TAG_PREFIX:-}"
VALKEY_BUNDLE=valkey/valkey-bundle:9.1.3-trixie@sha256:dd30c59c2b2a598e83308095220a31d6d93d64c40a839c702cb1e98c8e220660
VALKEY=valkey/valkey:9.1.2-trixie@sha256:418652cfb58ef879d4978c33553735d7147016032d5aefaa14c828e611eb9dfd
REDIS=redis:8.10.2-trixie@sha256:6f81e8915c60b065a524e6967e0ad1c639ba6efa84d669f823683ea04d9150ee
OLD=redis:7.2.16@sha256:0637954999d01b7c9ce9167db2da50656e2590d3b884f1c600c5f63bb6e6773c
for i in "$VALKEY_BUNDLE" "$VALKEY" "$REDIS" "$OLD"; do docker pull -q "$i" >/dev/null; done
docker build -q -f Dockerfile.app --build-arg BASE="$VALKEY_BUNDLE" --build-arg PRODUCT=valkey -t "${P}cari-kv-app:valkey-9.1" . >/dev/null
docker build -q -f Dockerfile.app --build-arg BASE="$REDIS"         --build-arg PRODUCT=redis  -t "${P}cari-kv-app:redis-8.10" . >/dev/null
docker build -q -f Dockerfile.mig --build-arg BASE="$VALKEY"        --build-arg PRODUCT=valkey -t "${P}cari-kv-mig:valkey-9.1" . >/dev/null
docker build -q -f Dockerfile.mig --build-arg BASE="$REDIS"         --build-arg PRODUCT=redis  -t "${P}cari-kv-mig:redis-8.10" . >/dev/null
docker build -q -f Dockerfile.mig8 -t "${P}cari-kv-mig8:valkey-9.1" . >/dev/null
python3 - "$P" <<'PY'
import json, subprocess, sys
prefix = sys.argv[1]
def sh(*a): return subprocess.run(a, capture_output=True, text=True, check=True).stdout.strip()
def pips(tag, path, names):
    return {p["name"]: p["version"] for p in json.loads(sh("docker", "run", "--rm", tag, "cat", path)) if p["name"].lower() in names}
out = {"images": {}}
for name in ["cari-kv-app:valkey-9.1", "cari-kv-app:redis-8.10", "cari-kv-mig:valkey-9.1", "cari-kv-mig:redis-8.10", "cari-kv-mig8:valkey-9.1"]:
    tag = prefix + name
    prod = name.split(":")[1].split("-")[0]
    out["images"][name] = {
        "id": sh("docker", "image", "inspect", "-f", "{{.Id}}", tag),
        "server": sh("docker", "run", "--rm", tag, "sh", "-c", f"{prod}-server --version"),
        "old_server": sh("docker", "run", "--rm", tag, "sh", "-c",
                         "for b in /opt/redis-7.2/bin/redis-server /opt/redis-8.10/bin/redis-server; do [ -x $b ] && $b --version; done; true") or None,
        "pip": pips(tag, "/opt/pip-system.json", ("redis", "valkey", "numpy")),
        "pip_grader": pips(tag, "/opt/pip-grader.json", ("redis",)),
    }
# start each image as the runner does and wait for its servers (catches an image that cannot start)
import time
for name in out["images"]:
    cid = sh("docker", "run", "-d", "--rm", prefix + name)
    ready = False
    for _ in range(150):
        if subprocess.run(["docker", "exec", cid, "test", "-f", "/run/kv-ready"], capture_output=True).returncode == 0:
            ready = True
            break
        st = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", cid], capture_output=True, text=True)
        if st.returncode != 0 or st.stdout.strip() != "true":
            break   # exited (and, with --rm, already removed)
        time.sleep(0.4)
    out["images"][name]["starts"] = ready
    subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
    if not ready:
        print(f"FAIL {name}: the image does not start its servers")
json.dump(out, open("images.local.json", "w"), indent=1)
published = json.load(open("images.json"))["images"]
strip = lambda v: (v or "").split(" build=")[0]   # the build id differs between builds; the version does not
diffs = 0
for name, mine in out["images"].items():
    pub = published.get(name, {})
    for field in ("server", "old_server", "pip", "pip_grader"):
        a, b = mine.get(field), pub.get(field)
        same = strip(a) == strip(b) if field.endswith("server") else a == b
        if not same:
            diffs += 1
            print(f"DIFF {name} {field}: built {a!r}, published {b!r}")
diffs += sum(1 for v in out["images"].values() if not v["starts"])
print(f"{len(out['images'])} images built; " + ("server versions and packages match the published build" if not diffs else f"{diffs} differences from the published build"))
sys.exit(1 if diffs else 0)
PY
