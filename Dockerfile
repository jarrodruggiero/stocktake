# Build from the repository root:
#   docker build -t stocktake .
#
# Multi-arch, the way the release workflow does it:
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t ghcr.io/jarrodruggiero/stocktake:X.Y.Z --push .
#
# Multi-stage so the test suite runs against the same layers the app ships from.
# `runtime` is the last stage and therefore the default target, so the
# production build is unchanged; docker only builds the stages its target
# depends on, so `test` is skipped there.
#   tests: docker build --target test -t stocktake-test . \
#          && docker run --rm stocktake-test
#
# Both base images are pinned BY DIGEST, not by tag. `python:3.12-slim` is a
# moving target — the same tag is a different image next month — and a build
# that cannot be reproduced cannot be audited after an incident. Bumping these
# is a deliberate commit, which is the point.
FROM python@sha256:4fad23465a06cc5149a541fbec6f87e234a64dc0550f6bfdd2d290d8f03240df AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# uv, also pinned by digest.
COPY --from=ghcr.io/astral-sh/uv@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded /uv /usr/local/bin/uv

# The unprivileged user is created HERE, before anything large exists, and
# every layer below is written as it.
#
# It used to be created in the runtime stage, which then ran
# `chown -R 1000:1000 /srv/stocktake` — and a recursive chown rewrites
# every file it touches into a new layer. With a 281 MB virtualenv above it,
# that shipped the virtualenv TWICE: 300 MB to build it and another 297 MB to
# change its ownership. It is what took the image from 680 MB to 1.04 GB, and
# it looked like new dependencies rather than a chown.
#
# The uid is fixed at 1000 rather than left to the distro because it has to be
# predictable from outside the container:
#
#   * Kubernetes — set `fsGroup: 1000` on the pod so the mounted PVC is
#     group-writable. Without it the volume arrives owned by root and the app
#     cannot create its database.
#   * Docker/compose — a named volume inherits the image's ownership and just
#     works. A BIND mount does not: `chown -R 1000:1000 ./data` on the host
#     first, or the same failure.
#
# /data and /config are created and owned here so the common case needs no
# intervention. /config matters as much as /data now that the first-run wizard
# WRITES config.yaml: left to Docker to create, a named volume mounted there
# arrives owned by root, the app cannot write it, and the wizard quietly
# degrades to "here is the YAML, save it yourself" on every install. Creating
# it in the image means the volume inherits uid 1000 and the wizard can save.
#
# A ConfigMap mount over the top is still read-only, and still handled — that
# is a deliberate deployment choice rather than an accident of ownership.

# Tesseract, for statements that arrive as scans. Local OCR is the only kind
# this app will ever do — a dividend statement carries a name, an address and a
# holder number, and the standing promise is that only ticker symbols leave the
# machine. See app/ocr.py.
#
# MEASURED at 107 MB (37 MB of it libicu, pulled in by leptonica; 15 MB the
# English language data). That is a real cost and it was weighed: the same
# measurement pass found this image was shipping its virtualenv twice, worth
# 297 MB, so OCR went in and the image still came out ~370 MB smaller than the
# version before it.
#
# Placed before the venv so it stays cached across app changes, and while still
# root — everything below runs as uid 1000.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# `/scratch` is created here for the same reason /data and /config are: the app
# runs as uid 1000, and `/` is root-owned, so it could not make a top-level
# directory for itself. It holds the visual mapper's rendered pages — see
# `imports.visual_dir` in app/settings.py — and it is DELIBERATELY not a volume.
# Anything here is disposable and must not be backed up.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin stocktake \
    && mkdir -p /srv/stocktake /data /config /scratch \
    && chown -R 1000:1000 /srv /data /config /scratch

USER 1000:1000
WORKDIR /srv/stocktake

# Dependencies come from uv.lock — the single source of truth. The list used to
# be hand-duplicated here with a "keep in step with pyproject.toml" comment,
# which is a drift bug waiting to happen: the image would install a different
# set from the one the tests ran against, and nothing would say so.
COPY --chown=1000:1000 pyproject.toml uv.lock ./
# --frozen: fail if the lock is out of date with pyproject rather than silently
# re-resolving, so a forgotten `uv lock` breaks the build instead of shipping
# unreviewed versions. --no-install-project: only the app's source is copied
# below, and installing it as a package would need a rebuild on every edit.
# --no-cache: uv keeps a download cache under ~/.cache/uv and hardlinks out of
# it. Hardlinks do not survive being committed to an image layer, so the cache
# ships as a second full copy of every wheel. Nothing in this image ever
# re-resolves, so the cache has no reader.
RUN uv sync --frozen --no-dev --no-install-project --no-cache

# The annotated default config, shipped so the first-run wizard has something
# to write from. Without it a fresh container has no config.yaml and no way to
# make one, which is the situation the wizard exists to fix.
COPY --chown=1000:1000 config.yaml ./config.default.yaml

# App source last: it changes most often, so everything above stays cached.
# Migrations live inside app/migrations so they ship with the source.
COPY --chown=1000:1000 app ./app
# `appkit` is a package in this repository rather than a dependency to resolve,
# so it is copied like any other source directory.
COPY --chown=1000:1000 appkit ./appkit
ENV PATH="/srv/stocktake/.venv/bin:$PATH"

# --------------------------------------------------------------------------- #
# test — the same image plus the suite. Never part of the shipped layers.
# --------------------------------------------------------------------------- #
FROM base AS test
RUN uv sync --frozen --no-install-project --no-cache
COPY --chown=1000:1000 tests ./tests
# Files the suite READS but the runtime image has no use for. Without them the
# containerised run is not the same run as the local one, which is the whole
# claim this stage makes — and it silently was not: the alert-rule tests and
# two config tests had been failing here while `pytest` passed on a laptop.
#
#   config.yaml      — the annotated default. `base` copies it as
#                      config.default.yaml, which is the name the app wants and
#                      not the one the tests look for.
#   deploy/          — the shipped manifests and Prometheus rules. The rule
#                      tests parse them; test_pagemap checks the manifests
#                      mount the scratch directory the app writes to.
#   docs/, AGENTS.md — the documentation tests: every setting appears in the
#                      configuration reference, every module on the
#                      architecture map, every route documented is served.
#   tools/           — test_branding.py imports tools/render_brand.py to prove
#                      every generated brand file still matches branding.py.
#                      It went missing once, and it did not fail loudly: the
#                      import error stopped COLLECTION, so the containerised
#                      run reported an error rather than the full pass count
#                      and was easy to read as a flake. This stage has silently
#                      stopped being the same run as the local one more than
#                      once — check here first when the two disagree.
COPY --chown=1000:1000 config.yaml ./config.yaml
COPY --chown=1000:1000 deploy ./deploy
COPY --chown=1000:1000 tools ./tools
COPY --chown=1000:1000 docs ./docs
COPY --chown=1000:1000 AGENTS.md ./AGENTS.md
# This file, because test_pagemap checks the image really does create the
# scratch directory the settings point at.
COPY --chown=1000:1000 Dockerfile ./Dockerfile
# Each test builds its database under pytest's tmp_path, so nothing is mounted.
CMD ["python", "-m", "pytest", "tests", "-q"]

# --------------------------------------------------------------------------- #
# runtime — what gets pushed. Last stage = the default build target.
# --------------------------------------------------------------------------- #
FROM base AS runtime

# Nothing to do here but declare the volume: the user, the ownership and the
# directories were all settled in `base`, before the virtualenv existed. See
# the comment there for why that ordering matters — doing it in this stage cost
# ~297 MB of duplicated layer.
VOLUME ["/data"]

EXPOSE 8000
# config.yaml is mounted from a ConfigMap at /config/config.yaml in-cluster;
# on compose/Unraid the wizard writes it there itself on first run.
# Migrations run at startup (appkit.upgrade_to_head) before serving.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
