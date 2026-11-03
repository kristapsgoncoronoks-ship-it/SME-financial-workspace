# Validate only the pinned source build's relevant Go BuildInfo records.
# The caller must capture successful `go version -m` output before invoking awk.
BEGIN {
    want["vcs.revision"] = "1.20"
    want["vcs.time"] = "2026-10-25T09:43:55Z"
    want["vcs.modified"] = "false"
    want["CGO_ENABLED"] = "0"
    want["GOOS"] = "linux"
}
function record(key, valid) {
    seen[key]++
    if (!valid) invalid = 1
}
$1 ~ /:$/ {
    record("header", NR == 1 && NF == 2 && $1 == "/out/gosu:" &&
           $2 ~ /^go[0-9]+\.[0-9]+(\.[0-9]+)?$/)
    next
}
$1 == "path" {
    record("path", NF == 2 && $2 == "github.com/tianon/gosu")
}
$1 == "mod" {
    record("mod", (NF == 3 || (NF == 4 && $4 ~ /^h1:/)) &&
           $2 == "github.com/tianon/gosu" && $3 == "v1.20.0")
}
$1 == "dep" && $2 == "github.com/moby/sys/user" {
    record("moby", NF == 4 && $3 == "v0.4.1" && $4 ~ /^h1:/)
}
$1 == "=>" { invalid = 1 }
$1 == "build" {
    key = $2
    sub(/=.*/, "", key)
    if (key in want) {
        value = $2
        sub(/^[^=]*=/, "", value)
        record(key, NF == 2 && value == want[key])
    }
}
END {
    if (seen["header"] != 1 || seen["path"] != 1 || seen["mod"] != 1 || seen["moby"] != 1)
        invalid = 1
    for (key in want)
        if (seen[key] != 1) invalid = 1
    exit invalid ? 1 : 0
}
