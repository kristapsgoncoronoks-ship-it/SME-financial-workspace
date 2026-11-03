# Rebuild the published gosu 1.20 release with a supported patched Go compiler.
# The official PG16 base currently embeds Go1.24.6 in its privilege-drop helper.
# No PostgreSQL binary, init script, auth default, UID/GID or PGDATA is changed.
# Upstream source: https://github.com/tianon/gosu/tree/29b939aa5bd4dc588af765b8248f6e39150d2565
# gosu: Apache-2.0; upstream module checksums/dependencies are verified by Go.
# Match upstream's release-metadata process with the pinned fake-git helper.
# Release 1.20 is normalized to Go semver v1.20.0, matching upstream revision metadata.
# The exact source SHA is pinned above; the source commit timestamp is stamped below.
FROM golang:1.27.3-alpine3.24@sha256:f2c872bcee949b7be9c490efd02b48a8a2fbf947cdf533f1c58525ec9d8170fd AS build
COPY --chmod=0644 verify-gosu-buildinfo.awk /usr/local/share/gosu/verify-buildinfo.awk
# Apply compatible libc/utilities and compression maintenance before fetching
# source or running the pinned compiler and helper release.
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    set -eu; \
    apk add --no-cache --upgrade curl bash \
        'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' 'zlib>=1.3.2-r1'; \
    curl --fail --location --proto =https --tlsv1.2 --output /tmp/gosu.tar.gz https://codeload.github.com/tianon/gosu/tar.gz/29b939aa5bd4dc588af765b8248f6e39150d2565; \
    echo '2b70da8a5480fafedc16bb1fc91b572f064d03df736cbd91a616af2f1b66ab51  /tmp/gosu.tar.gz' | sha256sum -c -; \
    mkdir /src; tar -xf /tmp/gosu.tar.gz -C /src --strip-components=1; \
    curl --fail --location --proto =https --tlsv1.2 --output /tmp/fake-git.tar.gz https://codeload.github.com/tianon/fake-git/tar.gz/aaa875961611764a86a21cf80c250929a7786179; \
    echo '4eebe35bfc2842398d325c09167faa597cd4c29f7049ae239b13822d20dfbf88  /tmp/fake-git.tar.gz' | sha256sum -c -; \
    mkdir /opt/fake-git; tar -xf /tmp/fake-git.tar.gz -C /opt/fake-git --strip-components=1; \
    install -m 0755 /opt/fake-git/fake-git.sh /usr/local/bin/git; \
    mkdir /src/.git; \
    export FAKEGIT_GO_SEMVER=v1.20.0 \
        FAKEGIT_GO_REVISION=1.20 \
        FAKEGIT_GO_TIMESTAMP=1792921435; \
    cd /src; CGO_ENABLED=0 GOTOOLCHAIN=local go build -mod=readonly -trimpath -ldflags='-d -w' -buildvcs=true -o /out/gosu .; \
    go version -m /out/gosu > /out/gosu-buildinfo.txt; \
    awk -f /usr/local/share/gosu/verify-buildinfo.awk /out/gosu-buildinfo.txt; \
    /out/gosu --version; /out/gosu nobody id; cat /out/gosu-buildinfo.txt; \
    mkdir -p /out/licenses; \
    cp /out/gosu-buildinfo.txt /out/licenses/gosu-buildinfo.txt; \
    cp /src/LICENSE /out/licenses/gosu-APACHE-2.0; \
    cp /opt/fake-git/LICENSE /out/licenses/fake-git-APACHE-2.0; \
    cp /usr/local/go/LICENSE /out/licenses/Go-BSD-3-Clause; \
    cp /go/pkg/mod/github.com/moby/sys/user@v0.4.1/LICENSE /out/licenses/moby-sys-user-APACHE-2.0; \
    cp /go/pkg/mod/golang.org/x/sys@v0.1.0/LICENSE /out/licenses/golang-x-sys-BSD-3-Clause; \
    rm /tmp/gosu.tar.gz /tmp/fake-git.tar.gz

# Inherit ENTRYPOINT/CMD, SIGINT, port5432, UID/GID70 and the PG16 data volume.
FROM postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea AS runtime
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    apk add --no-cache --upgrade \
        'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' 'zlib>=1.3.2-r1'
COPY --from=build --chmod=0755 /out/gosu /usr/local/bin/gosu
COPY --from=build /out/licenses/ /usr/share/licenses/gosu/
RUN gosu --version && gosu postgres id && su-exec nobody true && postgres --version
