# Rebuild gosu 1.20 with the patched compiler and a pinned downstream manifest.
# The official PG16 base currently embeds Go1.24.6 in its privilege-drop helper.
# No PostgreSQL binary, init script, auth default, UID/GID or PGDATA is changed.
# Upstream source: https://github.com/tianon/gosu/tree/29b939aa5bd4dc588af765b8248f6e39150d2565
# gosu: Apache-2.0; upstream module checksums/dependencies are verified by Go.
# Match upstream's release-metadata process with the pinned fake-git helper.
# Release 1.20 is normalized to Go semver v1.20.0, matching upstream revision metadata.
# The exact source SHA is pinned above; the source commit timestamp is stamped below.
# Only go.mod/go.sum change: official x/sys 0.49.0 requires root Go 1.26.0.
# The real BuildInfo must say v1.20.0+dirty and identify the exact module overlay.
FROM golang:1.27.3-alpine3.24@sha256:f2c872bcee949b7be9c490efd02b48a8a2fbf947cdf533f1c58525ec9d8170fd AS build
ARG TARGETARCH
COPY --chmod=0644 verify-gosu-buildinfo.awk /usr/local/share/gosu/verify-buildinfo.awk
COPY --chmod=0644 gosu-go.mod /tmp/gosu-go.mod
COPY --chmod=0644 gosu-go.sum /tmp/gosu-go.sum
# Apply compatible libc/utilities and compression maintenance before fetching
# source or running the pinned compiler and helper release.
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    set -eu; \
    apk add --no-cache --upgrade curl bash \
        'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' 'zlib>=1.3.2-r1'; \
    : "${TARGETARCH:?BuildKit TARGETARCH is required for gosu provenance}"; \
    curl --fail --location --proto =https --tlsv1.2 --output /tmp/gosu.tar.gz https://codeload.github.com/tianon/gosu/tar.gz/29b939aa5bd4dc588af765b8248f6e39150d2565; \
    echo '2b70da8a5480fafedc16bb1fc91b572f064d03df736cbd91a616af2f1b66ab51  /tmp/gosu.tar.gz' | sha256sum -c -; \
    mkdir /src; tar -xf /tmp/gosu.tar.gz -C /src --strip-components=1; \
    echo '216dbdc31af7371e368b90a85837c6dcf8aa7139cb449ecceaa0526d91900c3a  /src/go.mod' | sha256sum -c -; \
    echo '3f4d244f09dc5009ad68d99b2d99815f48fddf693fad8e734bbe820b315f43e7  /src/go.sum' | sha256sum -c -; \
    echo '4632cb07b230b1caee8f377dae2f28e5d66757d1867d3d783aa84166620ff4f8  /tmp/gosu-go.mod' | sha256sum -c -; \
    echo '4c5ca63607e11c08110f8b8326552b4c62090a736ecf8ce03188db600dedc515  /tmp/gosu-go.sum' | sha256sum -c -; \
    cp /tmp/gosu-go.mod /src/go.mod; \
    cp /tmp/gosu-go.sum /src/go.sum; \
    curl --fail --location --proto =https --tlsv1.2 --output /tmp/fake-git.tar.gz https://codeload.github.com/tianon/fake-git/tar.gz/aaa875961611764a86a21cf80c250929a7786179; \
    echo '4eebe35bfc2842398d325c09167faa597cd4c29f7049ae239b13822d20dfbf88  /tmp/fake-git.tar.gz' | sha256sum -c -; \
    mkdir /opt/fake-git; tar -xf /tmp/fake-git.tar.gz -C /opt/fake-git --strip-components=1; \
    install -m 0755 /opt/fake-git/fake-git.sh /usr/local/bin/git; \
    mkdir /src/.git; \
    export FAKEGIT_GO_SEMVER=v1.20.0 \
        FAKEGIT_GO_REVISION=1.20 \
        FAKEGIT_GO_TIMESTAMP=1792921435 \
        FAKEGIT_GO_MODIFIED="$(printf ' M go.mod\n M go.sum')"; \
    cd /src; CGO_ENABLED=0 GOTOOLCHAIN=local go build -mod=readonly -trimpath -ldflags='-d -w' -buildvcs=true -o /out/gosu .; \
    go version -m /out/gosu > /out/gosu-buildinfo.txt; \
    awk -v expected_goarch="$TARGETARCH" -f /usr/local/share/gosu/verify-buildinfo.awk /out/gosu-buildinfo.txt; \
    mkdir -p /out/licenses; \
    cp /out/gosu-buildinfo.txt /out/licenses/gosu-buildinfo.txt; \
    cp /src/LICENSE /out/licenses/gosu-APACHE-2.0; \
    cp /opt/fake-git/LICENSE /out/licenses/fake-git-APACHE-2.0; \
    cp /usr/local/go/LICENSE /out/licenses/Go-BSD-3-Clause; \
    cp /go/pkg/mod/github.com/moby/sys/user@v0.4.1/LICENSE /out/licenses/moby-sys-user-APACHE-2.0; \
    echo '911f8f5782931320f5b8d1160a76365b83aea6447ee6c04fa6d5591467db9dad  /go/pkg/mod/golang.org/x/sys@v0.49.0/LICENSE' | sha256sum -c -; \
    cp /go/pkg/mod/golang.org/x/sys@v0.49.0/LICENSE /out/licenses/golang-x-sys-BSD-3-Clause; \
    cp /src/go.mod /out/licenses/gosu-go.mod; \
    cp /src/go.sum /out/licenses/gosu-go.sum; \
    printf '%s\n' \
        'upstream_gosu_commit=29b939aa5bd4dc588af765b8248f6e39150d2565' \
        'upstream_gosu_archive_sha256=2b70da8a5480fafedc16bb1fc91b572f064d03df736cbd91a616af2f1b66ab51' \
        'upstream_go_mod_sha256=216dbdc31af7371e368b90a85837c6dcf8aa7139cb449ecceaa0526d91900c3a' \
        'upstream_go_sum_sha256=3f4d244f09dc5009ad68d99b2d99815f48fddf693fad8e734bbe820b315f43e7' \
        'overlay_go_mod_sha256=4632cb07b230b1caee8f377dae2f28e5d66757d1867d3d783aa84166620ff4f8' \
        'overlay_go_sum_sha256=4c5ca63607e11c08110f8b8326552b4c62090a736ecf8ce03188db600dedc515' \
        'overlay_module=golang.org/x/sys@v0.49.0' \
        'source_modified=true' > /out/licenses/gosu-source-provenance.txt; \
    chmod 0755 /out/licenses; chmod 0644 /out/licenses/*; \
    /out/gosu --version; /out/gosu nobody id; cat /out/gosu-buildinfo.txt; \
    rm /tmp/gosu.tar.gz /tmp/fake-git.tar.gz

# Inherit ENTRYPOINT/CMD, SIGINT, port5432, UID/GID70 and the PG16 data volume.
FROM postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea AS runtime
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444 \
    apk add --no-cache --upgrade \
        'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' 'zlib>=1.3.2-r1' \
        'krb5-libs>=1.22.3-r0' 'libxslt>=1.1.43-r4'
COPY --from=build --chmod=0755 /out/gosu /usr/local/bin/gosu
COPY --from=build /out/licenses/ /usr/share/licenses/gosu/
RUN gosu --version && gosu postgres id && su-exec nobody true && postgres --version
