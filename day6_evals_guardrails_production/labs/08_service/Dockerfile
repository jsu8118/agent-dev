# Kestrel support-agent service - container image.
#
# Build from the REPOSITORY ROOT (the image needs labkit/, kestrel/ and data/ next to the service):
#   docker build -f day6_evals_guardrails_production/labs/08_service/Dockerfile -t kestrel-support:dev .
# Run against the live API (the key comes from your environment or a secret store, never from the image):
#   docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY kestrel-support:dev
# Run fully offline against the course's mock:
#   docker run --rm -p 8000:8000 -e LABKIT_MODE=mock kestrel-support:dev
# Then: curl -s localhost:8000/healthz
#
# Dockerfile.dockerignore (next to this file) keeps .venv, .git and .runs out of the build context.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LABKIT_QUIET=1

WORKDIR /srv

# Behind a TLS-inspecting corporate proxy? Pass its CA certificate (PEM text) so pip and the SDK trust it
# (same convention as the course image in the repository root). Never disable TLS verification instead.
#   docker build --build-arg EXTRA_CA_CERT="$(cat /path/to/proxy-ca.pem)" ...        Empty by default.
ARG EXTRA_CA_CERT=""
RUN if [ -n "$EXTRA_CA_CERT" ]; then printf '%s\n' "$EXTRA_CA_CERT" > /usr/local/share/ca-certificates/extra-ca.crt \
        && update-ca-certificates; fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# Dependencies first, so code edits do not invalidate this layer.  Editable install on purpose: labkit
# resolves data/ and .runs/ relative to its source tree.
COPY pyproject.toml ./
COPY labkit ./labkit
COPY kestrel ./kestrel
RUN pip install -e ".[service]"

COPY data ./data
COPY day6_evals_guardrails_production/labs/_*.py ./day6_evals_guardrails_production/labs/
COPY day6_evals_guardrails_production/labs/08_service/app.py ./day6_evals_guardrails_production/labs/08_service/

# Run as an unprivileged user; the only writable path is the scratch directory.
RUN useradd --create-home --uid 10001 kestrel && mkdir -p /srv/.runs && chown -R kestrel /srv/.runs
USER kestrel

EXPOSE 8000

# Liveness/readiness without calling the model (see /healthz).  slim images have no curl.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

# Two worker processes; each has its own concurrency limit (KESTREL_MAX_CONCURRENCY) and metrics.
# Scale further with more replicas behind a load balancer - the workers keep no conversation state.
CMD ["uvicorn", "app:app", "--app-dir", "day6_evals_guardrails_production/labs/08_service", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--timeout-graceful-shutdown", "30"]
