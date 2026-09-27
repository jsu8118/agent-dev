# Course image: everything needed to run every lab, solution and test.
#   docker build -t claude-agent-course .
#   docker run --rm claude-agent-course                     # runs the whole test suite in mock mode
#   docker run --rm -it --env-file .env claude-agent-course bash
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# No system packages needed: the official slim image already ships ca-certificates (TLS to
# api.anthropic.com in live mode), and the claude-agent-sdk wheel bundles the Claude Code CLI
# binary used on Day 5, so Node.js is not required either.

# Behind a TLS-inspecting corporate proxy? Pass its CA certificate (PEM text) so pip and the SDK trust it:
#   docker build --build-arg EXTRA_CA_CERT="$(cat /path/to/proxy-ca.pem)" -t claude-agent-course .
# (Never disable TLS verification instead.)
ARG EXTRA_CA_CERT=""
RUN if [ -n "$EXTRA_CA_CERT" ]; then \
        printf '%s\n' "$EXTRA_CA_CERT" > /usr/local/share/ca-certificates/extra-ca.crt && update-ca-certificates; \
    fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /course

# Install dependencies first (better layer caching), including the editable course packages.
COPY requirements.txt pyproject.toml ./
COPY labkit ./labkit
COPY kestrel ./kestrel
RUN pip install -r requirements.txt

COPY . .

# Run as an unprivileged user; labs write scratch files under .runs/
RUN useradd --create-home learner && mkdir -p .runs && chown -R learner /course
USER learner

CMD ["python", "-m", "pytest", "-q"]
