# CARI Issue 2 - cache + vector jobs. One file, two products: BASE is the official image, PRODUCT names it.
#   valkey: BASE=valkey/valkey-bundle:9.1.3-trixie (Valkey 9.1.2 + json/search/bloom/ldap modules)
#   redis:  BASE=redis:8.10.2-trixie              (Redis 8.10.2 + search/json/bloom/timeseries modules)
ARG BASE
FROM ${BASE}
ARG PRODUCT
USER root
ENV DEBIAN_FRONTEND=noninteractive PIP_BREAK_SYSTEM_PACKAGES=1 PIP_DISABLE_PIP_VERSION_CHECK=1 KV_PRODUCT=${PRODUCT}
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv ca-certificates curl jq procps less vim-tiny iproute2 net-tools \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /bin/bash /bin/sh
# client libraries an engineer would reach for, for BOTH products (the agent chooses)
RUN pip3 install --no-cache-dir redis valkey numpy && pip3 list --format=json > /opt/pip-system.json
# the grader's own environment (agent-independent)
RUN python3 -m venv /opt/kv-grader && /opt/kv-grader/bin/pip install --no-cache-dir redis \
    && /opt/kv-grader/bin/pip list --format=json > /opt/pip-grader.json
COPY assets/app/ /app/
COPY assets/upstream/ /opt/upstream/
COPY assets/data/ /data/
COPY assets/conf/${PRODUCT}.conf /etc/${PRODUCT}/${PRODUCT}.conf
COPY assets/kv-start-app.sh /usr/local/sbin/kv-start
WORKDIR /root
ENTRYPOINT []
CMD ["/usr/local/sbin/kv-start"]
