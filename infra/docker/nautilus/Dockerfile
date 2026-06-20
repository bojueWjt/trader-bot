# syntax=docker/dockerfile:1.7

FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/home/nautilus/.local/bin:${PATH}"

RUN groupadd --system nautilus \
    && useradd --system --gid nautilus --home-dir /home/nautilus --create-home nautilus

WORKDIR /app

COPY infra/docker/nautilus/requirements.node.lock.txt /tmp/requirements.node.lock.txt

RUN test -s /tmp/requirements.node.lock.txt \
    && ! grep -q "Host-generated placeholder" /tmp/requirements.node.lock.txt \
    && python -m pip install --require-hashes -r /tmp/requirements.node.lock.txt

COPY services/nautilus-node/ /app/
COPY packages/execution-domain/execution_domain/ /app/execution_domain/
COPY packages/nautilus-adapter/intent/ /app/intent/

RUN mkdir -p /var/lib/nautilus-node/spool /app/bin \
    && printf '%s\n' '#!/bin/sh' 'exec python -m app.run_node "$@"' > /app/bin/run-nautilus-node \
    && chmod 0755 /app/bin/run-nautilus-node \
    && chown -R nautilus:nautilus /app /var/lib/nautilus-node

ENV PYTHONPATH=/app \
    PIP_NO_INDEX=1

USER nautilus

ENTRYPOINT ["/app/bin/run-nautilus-node"]
