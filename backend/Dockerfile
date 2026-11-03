# ---------- build stage: install dependencies so the runtime image stays lean ----------
FROM python:3.14-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72 AS build

ENV PYTHONDONTWRITEBYTECODE=1 UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Use the maintained native installer instead of the base image's pip, whose
# vendored dependencies remain vulnerable even in its latest upstream release.
# Remove that unused installer before dependency processing, not after its use.
# uv is MIT/Apache-2.0 licensed; its executable remains in the audited build stage.
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
RUN rm -rf /usr/local/lib/python3.14/site-packages/pip \
        /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
        /usr/local/lib/python3.14/ensurepip/_bundled \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.14

# Build toolchain only lives in this stage (not shipped in the final image).
# Require fixed Alpine libc/utilities and zlib before dependency processing;
# the matching runtime floors below retain the same supported release/ABI.
COPY --chmod=0644 scripts/build_system_expat.py scripts/verify_system_expat.py /usr/local/share/python-system-expat/
COPY --chmod=0644 build-licenses/python-system-expat/README.md build-licenses/python-system-expat/EXPAT-COPYING /usr/local/share/licenses/python-system-expat/
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    apk add --no-cache --upgrade build-base pkgconf \
        'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' 'zlib>=1.3.2-r1' \
        'libexpat>=2.10.0-r0' 'expat-dev>=2.10.0-r0' \
    && chmod 0755 /usr/local/share/python-system-expat /usr/local/share/licenses /usr/local/share/licenses/python-system-expat \
    && python -I -B /usr/local/share/python-system-expat/build_system_expat.py

COPY requirements.txt .
# Optional public CA bundle for corporate build proxies; never stored in the image.
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv pip install --python /usr/local/bin/python --prefix /opt/backend \
        --link-mode copy --no-python-downloads -r requirements.txt
COPY build-licenses/uv/ /usr/local/share/licenses/uv/

# ---------- runtime stage ----------
FROM python:3.14-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72 AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Tunable at deploy time (see docs/DEPLOYMENT.md).
    WEB_CONCURRENCY=4

WORKDIR /app

# Runtime OS deps: tesseract for the OCR fallback; tini for correct signal
# handling (clean SIGTERM → graceful shutdown, no zombie procs). Python owns
# the health probe, so a separate curl executable is unnecessary. tzdata supplies
# IANA zones for assignment validation, local calendar dates and DST transitions.
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    apk add --no-cache --upgrade \
        'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' 'zlib>=1.3.2-r1' \
        'libexpat>=2.10.0-r0' \
        tesseract-ocr tesseract-ocr-data-eng tesseract-ocr-data-osd \
        font-dejavu tini tzdata gcompat libstdc++ \
    && mkdir -p /usr/share/fonts/truetype \
    && ln -s /usr/share/fonts/dejavu /usr/share/fonts/truetype/dejavu

# Copy the installed prefix built for this exact Python/Alpine base. The runtime
# needs neither an installer nor a compiler and performs no package-network I/O.
RUN rm -rf /usr/local/lib/python3.14/site-packages/pip \
        /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
        /usr/local/lib/python3.14/ensurepip/_bundled \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.14
COPY --from=build /opt/python-system-expat/lib-dynload/ /usr/local/lib/python3.14/lib-dynload/
COPY --from=build /opt/python-system-expat/provenance/ /usr/local/share/licenses/python-system-expat/
COPY --chmod=0644 scripts/verify_system_expat.py /usr/local/share/python-system-expat/verify_system_expat.py
COPY --chmod=0644 build-licenses/python-system-expat/README.md build-licenses/python-system-expat/EXPAT-COPYING /usr/local/share/licenses/python-system-expat/
COPY --from=build /opt/backend/ /usr/local/
COPY requirements.txt .

COPY . .

# Run as an unprivileged user. Preserve the shared-volume UID/GID contract.
# Docker copies the target owner/mode into an empty named volume; existing
# nonempty volumes keep their operator-managed ownership.
RUN chmod 0755 /usr/local/share/python-system-expat /usr/local/share/licenses /usr/local/share/licenses/python-system-expat \
    && addgroup -g 10001 appuser && adduser -D -u 10001 -G appuser appuser \
    && mkdir -p /app/var/storage \
    && chmod 0700 /app/var/storage \
    && chown -R appuser:appuser /app
USER appuser

# Verify the fully assembled prefix and paired extensions with runtime permissions.
RUN python -I -B /usr/local/share/python-system-expat/verify_system_expat.py

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
