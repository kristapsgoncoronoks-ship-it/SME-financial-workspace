# ---------- build stage: install dependencies so the runtime image stays lean ----------
FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd AS build

ENV PYTHONDONTWRITEBYTECODE=1 UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Use the maintained native installer instead of the base image's pip, whose
# vendored dependencies remain vulnerable even in its latest upstream release.
# Remove that unused installer before dependency processing, not after its use.
# uv is MIT/Apache-2.0 licensed; its executable remains in the audited build stage.
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /usr/local/bin/uv
RUN rm -rf /usr/local/lib/python3.14/site-packages/pip \
        /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
        /usr/local/lib/python3.14/ensurepip/_bundled \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.14

# Build toolchain only lives in this stage (not shipped in the final image).
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    apk add --no-cache build-base

COPY requirements.txt .
# Optional public CA bundle for corporate build proxies; never stored in the image.
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv pip install --python /usr/local/bin/python --prefix /opt/backend \
        --link-mode copy --no-python-downloads -r requirements.txt
COPY build-licenses/uv/ /usr/local/share/licenses/uv/

# ---------- runtime stage ----------
FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Tunable at deploy time (see docs/DEPLOYMENT.md).
    WEB_CONCURRENCY=4

WORKDIR /app

# Runtime OS deps: tesseract for the OCR fallback; tini for correct signal
# handling (clean SIGTERM → graceful shutdown, no zombie procs). Python owns
# the health probe, so a separate curl executable is unnecessary.
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    apk add --no-cache tesseract-ocr tesseract-ocr-data-eng tesseract-ocr-data-osd \
        font-dejavu tini gcompat libstdc++ \
    && mkdir -p /usr/share/fonts/truetype \
    && ln -s /usr/share/fonts/dejavu /usr/share/fonts/truetype/dejavu

# Copy the installed prefix built for this exact Python/Alpine base. The runtime
# needs neither an installer nor a compiler and performs no package-network I/O.
RUN rm -rf /usr/local/lib/python3.14/site-packages/pip \
        /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
        /usr/local/lib/python3.14/ensurepip/_bundled \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.14
COPY --from=build /opt/backend/ /usr/local/
COPY requirements.txt .

COPY . .

# Run as an unprivileged user. Preserve the shared-volume UID/GID contract.
# Docker copies the target owner/mode into an empty named volume; existing
# nonempty volumes keep their operator-managed ownership.
RUN addgroup -g 10001 appuser && adduser -D -u 10001 -G appuser appuser \
    && mkdir -p /app/var/storage \
    && chmod 0700 /app/var/storage \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Liveness for orchestrators that honour it; k8s uses its own probes (see manifests).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3).close()" || exit 1

# tini as PID 1 forwards signals so uvicorn shuts down gracefully. Worker count
# is WEB_CONCURRENCY (env).
#
# SEC-002: NO --proxy-headers here. `--forwarded-allow-ips '*'` made uvicorn
# copy the LEFTMOST, client-supplied X-Forwarded-For value into scope["client"],
# which is exactly the address the app's own spoofing-resistant rule
# (app/core/ratelimit.py::_client_ip, TRUSTED_PROXY_COUNT hops from the RIGHT)
# then trusted as "the socket peer" — so a forged header minted a fresh
# auth rate-limit bucket per request and forged the audit trail's ip column.
# The app reads X-Forwarded-For and X-Forwarded-Proto itself; set
# TRUSTED_PROXY_COUNT to the real number of hops (nginx origin = 1).
ENTRYPOINT ["tini", "--"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-4}"]
