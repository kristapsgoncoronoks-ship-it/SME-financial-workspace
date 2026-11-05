# Backend dependency graphs and runtime smoke

`backend/requirements-runtime.txt` contains application and operational dependencies.
`requirements-test.txt` contains pytest tooling. The existing `requirements.txt`
entry point includes both, and `requirements-dev.txt` adds the developer tools.
Install the full local environment with `pip install -r requirements-dev.txt`.
Each package version is declared in one manifest. PostgreSQL, OCR, HTTPX,
Prometheus, PyYAML and their runtime dependencies remain available in the image.

The production Dockerfile installs only the runtime graph. Its final image has
no pytest distributions, compiler, uv or pip. Run the in-container pytest smoke
through a disposable environment over that exact runtime image:

```bash
# Use runtime/build images produced from the same reviewed source.
# Preparation downloads only pure-Python wheels on the host and verifies their
# SHA256 against published PyPI release metadata. Use a fresh directory.
python scripts/backend_runtime_smoke.py prepare \
  --image bidit-ci-backend:review --wheelhouse /tmp/bidit-smoke-wheels

python scripts/backend_runtime_smoke.py run \
  --image bidit-ci-backend:review \
  --builder-image bidit-ci-backend-build:review \
  --wheelhouse /tmp/bidit-smoke-wheels -- tests/test_fx.py tests/test_jobs.py -q
```

The host freezes both image IDs and obtains the pinned uv executable from the
audited build image. The smoke container retains UID/GID 10001, disables networking,
mounts a hash-checked read-only copy of the source tests, and creates a temporary
venv with access to the image's installed packages.
Hash-locked test tools install offline from the read-only wheelhouse; existing
runtime distributions must retain their versions and locations. Dependency checks
and pytest failures propagate to the caller. Container and venv cleanup leave the
production image unchanged. Other self-contained test selections can replace the
two paths above; repository/workflow inspection tests need the whole repository.
Native PostgreSQL selections may explicitly supply `--network` and a private
`--env-file` for a disposable test database. Wheel installation still uses
offline/no-index/hash-locked inputs, and no provider credentials are needed.

Base dependency review follows local `.txt` requirements and constraints within
`backend/`, preserving their relative layout from the exact base commit. Cycles,
remote/environment paths, symlinks, missing files and graphs beyond the explicit
file/depth/byte budgets fail the review. CI's `backend/**/*.txt` cache input covers
every supported local include. No audit exception or offline vulnerability-scan
fallback is introduced by this development smoke mechanism.

The earlier five-distribution probe measured about 5.86 MiB of logical files.
Compressed image size, application memory and latency require their own actual
measurements; removing test packages alone does not establish those benefits.
