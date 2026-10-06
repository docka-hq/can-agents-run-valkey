#!/usr/bin/env bash
# Build the five job images and record exactly what was built (images.json).
#   bash images/build.sh
set -euo pipefail
cd "$(dirname "$0")"
VALKEY_BUNDLE=valkey/valkey-bundle:9.1.3-trixie
VALKEY=valkey/valkey:9.1.2-trixie
REDIS=redis:8.10.2-trixie
OLD=redis:7.2.16
for i in "$VALKEY_BUNDLE" "$VALKEY" "$REDIS" "$OLD"; do docker pull -q "$i" >/dev/null; done
docker build -q -f Dockerfile.app --build-arg BASE="$VALKEY_BUNDLE" --build-arg PRODUCT=valkey -t cari-kv-app:valkey-9.1 . >/dev/null
docker build -q -f Dockerfile.app --build-arg BASE="$REDIS"         --build-arg PRODUCT=redis  -t cari-kv-app:redis-8.10 . >/dev/null
docker build -q -f Dockerfile.mig --build-arg BASE="$VALKEY"        --build-arg PRODUCT=valkey -t cari-kv-mig:valkey-9.1 . >/dev/null
docker build -q -f Dockerfile.mig --build-arg BASE="$REDIS"         --build-arg PRODUCT=redis  -t cari-kv-mig:redis-8.10 . >/dev/null
docker build -q -f Dockerfile.mig8 -t cari-kv-mig8:valkey-9.1 . >/dev/null
python3 - "$VALKEY_BUNDLE" "$VALKEY" "$REDIS" "$OLD" <<'PY'
import json, subprocess, sys
def sh(*a): return subprocess.run(a, capture_output=True, text=True, check=True).stdout.strip()
out = {"bases": {}, "images": {}}
for b in sys.argv[1:]:
    out["bases"][b] = sh("docker", "image", "inspect", "-f", "{{index .RepoDigests 0}}", b)
for tag in ["cari-kv-app:valkey-9.1", "cari-kv-app:redis-8.10", "cari-kv-mig:valkey-9.1", "cari-kv-mig:redis-8.10"]:
    prod = tag.split(":")[1].split("-")[0]
    ver = sh("docker", "run", "--rm", tag, "sh", "-c", f"{prod}-server --version")
    old = sh("docker", "run", "--rm", tag, "sh", "-c", "/opt/redis-7.2/bin/redis-server --version 2>/dev/null || true")
    pips = json.loads(sh("docker", "run", "--rm", tag, "cat", "/opt/pip-system.json"))
    out["images"][tag] = {"id": sh("docker", "image", "inspect", "-f", "{{.Id}}", tag), "server": ver,
                          "old_server": old or None,
                          "pip": {p["name"]: p["version"] for p in pips if p["name"].lower() in ("redis", "valkey", "numpy")}}
json.dump(out, open("images.json", "w"), indent=1)
print(json.dumps(out, indent=1))
PY
