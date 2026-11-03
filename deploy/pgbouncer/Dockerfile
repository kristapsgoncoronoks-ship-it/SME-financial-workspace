# The vendor's latest image still contains 1.26.0, which predates the
# CVE-2026-107350 SCRAM authentication fix. Keep its runtime/entrypoint contract
# and replace the binary with checksum-verified upstream 1.26.1 source.
FROM edoburu/pgbouncer:v1.26.0-p0@sha256:b17551c776ef7e5769ef80b956d20f85e2fd25dd8912d31d58f782aad495b711 AS build
USER root
WORKDIR /src

# The optional public CA is a build secret; TLS verification remains enabled.
# Require fixed transport libraries before fetching source: inherited packages
# must not satisfy an unversioned add while the audited builder stays vulnerable.
# Keep libc and its utilities on the same fixed Alpine maintenance floor.
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    apk add --no-cache --upgrade build-base \
        'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' \
        'libexpat>=2.10.0-r0' \
        'brotli-libs>=1.2.1-r0' 'curl>=8.23.0-r0' 'libcurl>=8.23.0-r0' \
        libevent-dev openssl-dev pkgconf \
    && curl --fail --show-error --location --proto '=https' --tlsv1.2 \
        https://www.pgbouncer.org/downloads/files/1.26.1/pgbouncer-1.26.1.tar.gz \
        --output pgbouncer.tar.gz \
    && echo '00886a2994b4844cd37110970df777772cfc42a74c400522c82fba509a3f42d9  pgbouncer.tar.gz' | sha256sum -c - \
    && tar -xzf pgbouncer.tar.gz \
    && cd pgbouncer-1.26.1 \
    && ./configure --prefix=/usr \
    && make -j2 pgbouncer \
    && strip pgbouncer \
    && ./pgbouncer --version | head -1 | grep -Fx 'PgBouncer 1.26.1'

# The build target is also audited: replace the inherited executable there,
# rather than leaving the vulnerable vendor binary installed beside the source.
RUN install -m 0755 /src/pgbouncer-1.26.1/pgbouncer /usr/bin/pgbouncer \
    && /usr/bin/pgbouncer --version | head -1 | grep -Fx 'PgBouncer 1.26.1'

FROM edoburu/pgbouncer:v1.26.0-p0@sha256:b17551c776ef7e5769ef80b956d20f85e2fd25dd8912d31d58f782aad495b711 AS runtime
USER root
# Match the current OpenSSL patch used by the build headers, retaining the
# vendor's Alpine release and all entrypoint/client-library contracts.
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    apk add --no-cache --upgrade libcrypto3 libssl3 \
        'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' 'zlib>=1.3.2-r1'
COPY --from=build /src/pgbouncer-1.26.1/pgbouncer /usr/bin/pgbouncer
COPY --from=build /src/pgbouncer-1.26.1/COPYRIGHT /usr/share/licenses/pgbouncer/COPYRIGHT
COPY --from=build /src/pgbouncer-1.26.1/lib/COPYRIGHT /usr/share/licenses/pgbouncer/libusual-COPYRIGHT
# Inherits the vendor's postgres UID/GID 70, entrypoint and client tools.
USER postgres
