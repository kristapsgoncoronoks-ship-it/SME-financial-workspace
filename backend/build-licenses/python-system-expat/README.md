# System Expat stdlib XML extensions

The existing pinned Python image determines PYTHON_VERSION and PYTHON_SHA256.
The builder obtains that exact released CPython source from python.org, verifies
its checksum, and uses the original configure arguments with --with-system-expat.
Only pyexpat and _elementtree are rebuilt together against Alpine system Expat;
the interpreter and other stdlib extensions remain unchanged. No CPython or
Expat source patch or floating upstream branch is used.

CPython's source LICENSE is copied from the verified archive into the generated
provenance directory. EXPAT-COPYING is the published Expat MIT notice from
https://github.com/libexpat/libexpat/blob/R_2_10_0/expat/COPYING. The installed
Alpine libexpat package retains its own package/version/license metadata.

The generated build.json records source URL/hash, configuration, ABI comparison,
two extension hashes and the native paired verification. Both extensions are
verified in a fresh process before replacement, and again after installation.
Each file replacement is atomic inside one Docker RUN; failed verification aborts
the build and never qualifies a fallback to the old parser. Runtime copies both
qualified files in one instruction and verifies them before app processing.

This uses CPython's supported system-Expat configuration and generated module
make targets, not a separate interpreter or hand-written compiler commands.
Alpine/musl and other architectures retain the pinned image's support limits;
actual qualification is recorded for linux/amd64. Compatible newer Expat runtime
minor/patch updates are allowed within the supported major when both compiled
clients remain coherent. No CVE-specific exploit or performance claim is made.
