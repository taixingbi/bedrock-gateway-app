# gateway-api image (V1 -- see docs/ROADMAP.md).
# Multi-stage: build deps in one layer, run as non-root in a slim final image.
# Designed to run on ECS Fargate (see section 2 of the architecture plan).
# policies/ is required at startup (FilePolicyStore reads
# policies/tenants.yaml, CertifiedRouter reads policies/route_sets.yaml)
# -- override TENANT_POLICY_PATH / ROUTE_SET_CONFIG_PATH if you mount
# your own instead of baking these defaults into the image.

FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml poetry.lock ./
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH
RUN python -m venv /opt/venv \
    && pip install --no-cache-dir 'poetry==2.1.4' \
    && poetry install --only main --no-root

FROM python:3.11-slim

RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --shell /bin/bash --create-home app

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY services/ ./services/
COPY policies/ ./policies/
RUN chown -R app:app /app

USER app
EXPOSE 8080

ENV GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=8080 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status==200 else 1)"

CMD ["uvicorn", "services.gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
