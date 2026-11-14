# Rebuild the current compiler release with its supported, patched Go toolchain.
# The npm release embeds an older Go runtime; keep the compiler source/version
# unchanged and update its vulnerable Unicode dependency through Go modules.
FROM golang:1.27.4-alpine@sha256:0a5f7fe796599ff487605987432e7619ae45beeb2fba4c2ce0b907879932aebf AS typescript-compiler
WORKDIR /src
ENV GOTOOLCHAIN=local
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    apk add --no-cache curl \
    && apk upgrade --no-cache \
    && curl --fail --location --retry 3 --output /tmp/typescript-go.tar.gz https://codeload.github.com/microsoft/typescript-go/tar.gz/2bd066d87f5bafd315be9f40889d0a60b9e58e0b \
    && echo '5ccb47dbb3f68cd0da58b71e6f445eee36a50bd3e9f9f330cc23a97b88500119  /tmp/typescript-go.tar.gz' | sha256sum -c - \
    && tar -xzf /tmp/typescript-go.tar.gz --strip-components=1 \
    && rm /tmp/typescript-go.tar.gz \
    && go get golang.org/x/text@v0.41.0 \
    && go mod verify \
    && mkdir -p /out/provenance \
    && CGO_ENABLED=0 GOMAXPROCS=2 go build -p=2 -trimpath -tags=noembed -ldflags='-s -w' -o /out/tsc ./cmd/tsgo \
    && cp LICENSE NOTICE.txt go.mod go.sum /out/provenance/ \
    && go version -m /out/tsc > /out/provenance/compiler-build.txt \
    && sha256sum /out/tsc > /out/provenance/compiler.sha256 \
    && printf '%s\n' 'source: https://github.com/microsoft/typescript-go/tree/2bd066d87f5bafd315be9f40889d0a60b9e58e0b' 'archive-sha256: 5ccb47dbb3f68cd0da58b71e6f445eee36a50bd3e9f9f330cc23a97b88500119' > /out/provenance/source.txt \
    && go clean -cache
# Carry license notices with the static compiler into the consuming Node stage.
# Discover its linked modules from the executable rather than maintaining a
# separate list that could silently miss a future transitive dependency.
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    set -eu; \
    copy_notices() { \
        origin="$1"; destination="$2"; \
        find "$origin" -type f \( -iname 'LICENSE*' -o -iname 'NOTICE*' -o -iname 'COPYING*' -o -iname 'COPYRIGHT*' -o -iname 'PATENTS*' \) > /tmp/bidit-notices; \
        test -s /tmp/bidit-notices; \
        while IFS= read -r file; do \
            relative="${file#"$origin"/}"; \
            mkdir -p "$destination/$(dirname "$relative")"; \
            cp "$file" "$destination/$relative"; \
        done < /tmp/bidit-notices; \
    }; \
    copy_notices /usr/local/go /out/provenance/licenses/go; \
    go version -m /out/tsc | awk '$1 == "dep" { print $2 }' > /tmp/bidit-modules; \
    test -s /tmp/bidit-modules; \
    while IFS= read -r module; do \
        module_dir="$(go list -m -f '{{.Dir}}' "$module")"; \
        test -n "$module_dir"; \
        copy_notices "$module_dir" "/out/provenance/licenses/modules/$module"; \
    done < /tmp/bidit-modules; \
    rm /tmp/bidit-notices /tmp/bidit-modules

# --- build stage ---
FROM node:22.23.4-alpine3.24@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS build
WORKDIR /app
# Keep the Node builder's installed zlib on the supported Alpine security floor
# before npm processes package archives. The optional public CA remains a secret.
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    apk add --no-cache --upgrade 'zlib>=1.4-r0'
COPY package*.json ./
COPY scripts/verify-npm-toolchain.mjs /tmp/bidit-verify-npm-toolchain.mjs
# Optional public CA bundle for corporate build proxies; never stored in the image.
# The official npm 12.2.2 parent fixes ip-address and includes the existing
# undici 6.29.1 repair. Pin its archive, then verify owner/path/resolution and
# exact parent bytes/notices. Apply the official compatible glob legacy patch
# separately; advisory metadata still includes it, so scanner findings stay visible.
RUN --mount=type=secret,id=build_ca \
    set -eu; \
    if [ -f /run/secrets/build_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; \
    npm pack --ignore-scripts --pack-destination /tmp npm@12.2.2; \
    node /tmp/bidit-verify-npm-toolchain.mjs archive /tmp/npm-12.2.2.tgz; \
    npm install --global --ignore-scripts --no-audit --no-fund /tmp/npm-12.2.2.tgz; \
    bidit_npm_root="$(npm root --global)"; \
    test "$bidit_npm_root" = /usr/local/lib/node_modules; \
    npm pack --ignore-scripts --pack-destination /tmp glob@13.0.7; \
    node /tmp/bidit-verify-npm-toolchain.mjs overlay "$bidit_npm_root" /tmp/glob-13.0.7.tgz; \
    node /tmp/bidit-verify-npm-toolchain.mjs installed "$bidit_npm_root" /usr/local/share/bidit-npm; \
    test "$(npm --version)" = 12.2.2; \
    rm /tmp/npm-12.2.2.tgz /tmp/glob-13.0.7.tgz /tmp/bidit-verify-npm-toolchain.mjs; \
    npm ci; \
    npm cache clean --force
COPY --from=typescript-compiler /out/tsc /tmp/bidit-tsc
COPY --from=typescript-compiler /out/provenance/ /usr/local/share/bidit-typescript/
# Preserve the official npm launcher and standard library, replacing only the
# native executable with the same compiler release rebuilt above. Deriving its
# path through the launcher supports every architecture supported by the package.
RUN node --input-type=module -e 'import assert from "node:assert/strict"; import fs from "node:fs"; import getExePath from "./node_modules/typescript/lib/getExePath.js"; assert.equal(JSON.parse(fs.readFileSync("node_modules/typescript/package.json", "utf8")).version, "7.0.2"); fs.copyFileSync("/tmp/bidit-tsc", getExePath()); fs.chmodSync(getExePath(), 0o755);' \
    && rm /tmp/bidit-tsc \
    && test "$(./node_modules/.bin/tsc --version)" = 'Version 7.0.2'
COPY . .
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

# --- serve stage ---
FROM nginx:stable-alpine@sha256:970364228b1e4f932abc22d321932816782cca8a20b85a77552a7cfe9acfb3b5
# Dev/default config; docker-compose.prod.yml mounts nginx.prod.conf (TLS) over it.
COPY --chmod=0644 nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
# Run the origin without root or Linux capabilities. Main PID/cache paths must
# remain writable; mounted production config/certificates stay read-only.
# Fixed libc/utilities and libxpm remain in the final-stage package inventory.
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    apk add --no-cache --upgrade \
        'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' 'libxpm>=3.5.20-r0' \
        'libexpat>=2.10.0-r0' 'libx11>=1.8.14-r0' 'brotli-libs>=1.2.1-r0' 'curl>=8.23.0-r0' 'libcurl>=8.23.0-r0' \
        'libxslt>=1.1.43-r4' \
    && sed -i -e 's|^[[:space:]]*pid[[:space:]][^;]*;|pid /tmp/nginx.pid;|' -e '/^user /d' /etc/nginx/nginx.conf \
    && chown -R nginx:nginx /var/cache/nginx
USER nginx
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -Y off -qO- http://127.0.0.1:80/ >/dev/null 2>&1 || exit 1
CMD ["nginx", "-g", "daemon off;"]
