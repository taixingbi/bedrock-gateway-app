# M0 walking-skeleton image for the gateway-api service.
# Multi-stage: build deps in one layer, run as non-root in a slim final image.
# Designed to run on ECS Fargate (see section 2 of the architecture plan).

FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim

RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --shell /bin/bash --create-home app

COPY --from=builder /install /usr/local
WORKDIR /app
COPY services/ ./services/

USER app
EXPOSE 8080

ENV GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=8080 \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status==200 else 1)"

CMD ["uvicorn", "services.gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
