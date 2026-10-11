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
# Every image is pinned BY DIGEST, with its tag beside it. The digest makes
# the build reproducible: `python:3.14-slim` is a different image next month,
# and a build that cannot be reproduced cannot be audited after an incident.
# The tag is what Dependabot follows. With the digest alone it followed
# `latest`, the full image built on buildpack-deps with compilers, git and
# curl: 415 MB compressed against 43 MB, three times, each in a pull request
# titled with two short digests. tests/test_image.py holds both rules.
#
# Slim also keeps the test stage honest: nothing needs a toolchain, because
# every dependency in uv.lock resolves from a wheel, and on slim one that
# started compiling from source fails the build rather than passing quietly.
# uv, pinned the same way, and on a FROM line of its own: Dependabot reads FROM
# lines and nothing else, so while uv was copied straight out of its image it
# was never proposed an update, and stayed at the version of the first commit.
FROM ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded AS uv

# --------------------------------------------------------------------------- #
# build — the virtualenv, made where git and uv are. Nothing below ships them.
# --------------------------------------------------------------------------- #
# Through Google's mirror of Docker Hub, as CI pulls everything: the same image
# by digest, without Docker Hub's rate limit on shared CI addresses.
FROM mirror.gcr.io/library/python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS build

# git, because appcore is a pinned git dependency and uv clones it. The full
# image had git, which is how a "slim" pin that kept turning full went
# unnoticed: on slim alone, `uv sync` cannot fetch appcore. Here only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /srv/stocktake
# Dependencies come from uv.lock — the single source of truth. The list used to
# be hand-duplicated here with a "keep in step with pyproject.toml" comment,
# which is a drift bug waiting to happen: the image would install a different
# set from the one the tests ran against, and nothing would say so.
COPY pyproject.toml uv.lock ./
# --frozen: fail if the lock is out of date with pyproject rather than silently
# re-resolving, so a forgotten `uv lock` breaks the build instead of shipping
# unreviewed versions. --no-install-project: only the app's source is copied
# below, and installing it as a package would need a rebuild on every edit.
# --no-cache: uv keeps a download cache under ~/.cache/uv and hardlinks out of
# it. Hardlinks do not survive being committed to an image layer, so the cache
# ships as a second full copy of every wheel. Nothing in this image ever
# re-resolves, so the cache has no reader.
RUN uv sync --frozen --no-dev --no-install-project --no-cache

# The same, with the development dependencies, for the test stage alone.
FROM build AS build-test
RUN uv sync --frozen --no-install-project --no-cache

# --------------------------------------------------------------------------- #
# base — what ships: the interpreter, OCR, the virtualenv and the source.
# --------------------------------------------------------------------------- #
FROM mirror.gcr.io/library/python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# The unprivileged user the app runs as, which owns only what the app writes:
# /data, /config and /scratch. The code and the virtualenv stay root's, so the
# process cannot rewrite itself.
#
# Never a recursive chown over the virtualenv: it rewrites every file it
# touches into a new layer. With a 281 MB virtualenv that once shipped it
# TWICE, 300 MB to build and another 297 MB to change its ownership, and it
# looked like new dependencies rather than a chown.
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
#
# Mode 1777, which is what /tmp is: writable by anyone, sticky so nobody can
# remove another user's files. /data and /config do NOT need it because they are
# always mounted over, and the mount's ownership is what applies — but /scratch
# is deliberately never mounted, so the image's own ownership is what the app
# meets. Owned by uid 1000, an operator running `--user 99:100` (which the
# Unraid template does, because appdata is nobody:users) could not create
# /scratch/visual and every visual statement import failed. Owning it 99:100
# instead would only move the failure onto uid 1000; 1777 works for whatever
# uid the operator picks.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin stocktake \
    && mkdir -p /srv/stocktake /data /config /scratch \
    && chown 1000:1000 /data /config \
    && chmod 1777 /scratch
WORKDIR /srv/stocktake

# The virtualenv from `build`, at the same path, on the same interpreter, and
# the files that describe it: pytest's settings live in pyproject.toml, and
# without them the test stage runs with no markers and no deselection.
COPY --from=build /srv/stocktake/.venv ./.venv
COPY pyproject.toml uv.lock ./
# For `alembic` run by hand, as docs/reference/cli.md shows. The app migrates
# itself at startup without it.
COPY alembic.ini ./

# The annotated default config, shipped so the first-run wizard has something
# to write from. Without it a fresh container has no config.yaml and no way to
# make one, which is the situation the wizard exists to fix.
COPY config.yaml ./config.default.yaml

# App source last: it changes most often, so everything above stays cached.
# Migrations live inside app/migrations so they ship with the source.
COPY app ./app
ENV PATH="/srv/stocktake/.venv/bin:$PATH"

# --------------------------------------------------------------------------- #
# test — the same image plus the suite. Never part of the shipped layers.
# --------------------------------------------------------------------------- #
FROM base AS test
COPY --from=build-test /srv/stocktake/.venv ./.venv
# The suite's own files are its user's, unlike the app's: the wizard tests save
# the suite's config.yaml, and a config nobody can write turns those steps off.
# Nothing in this stage ships.
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
#   README.md        — test_docs_match_the_code.py reads it with the docs:
#                      every path a page lints exists, and no line renders as
#                      a heading by accident.
#   mkdocs.yml       — test_doc_links.py: every "More information" link in the
#                      app resolves to a page that exists AND is in the nav.
#                      The nav half is the part that needs this file.
#   .github/         — test_workflows.py: every path a workflow lints has to
#                      exist, and the release has to lint what CI lints. Its
#                      own smoke test is what caught this omission — with the
#                      directory missing the sweep found no workflows, and a
#                      guard over nothing passes.
#   tools/           — test_branding.py imports tools/render_brand.py to prove
#                      every generated brand file still matches branding.py.
#                      It went missing once, and it did not fail loudly: the
#                      import error stopped COLLECTION, so the containerised
#                      run reported an error rather than the full pass count
#                      and was easy to read as a flake. This stage has silently
#                      stopped being the same run as the local one more than
#                      once — check here first when the two disagree.
COPY config.yaml ./config.yaml
COPY deploy ./deploy
COPY tools ./tools
COPY docs ./docs
COPY AGENTS.md README.md ./
COPY mkdocs.yml ./mkdocs.yml
COPY .github ./.github
# This file, because test_pagemap checks the image really does create the
# scratch directory the settings point at.
COPY Dockerfile .dockerignore ./
# Each test builds its database under pytest's tmp_path, so nothing is mounted.
# Run as the app's user, against code it cannot write, as it ships; so pytest
# keeps no cache in the working directory.
USER 1000:1000
CMD ["python", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]

# --------------------------------------------------------------------------- #
# runtime — what gets pushed. Last stage = the default build target.
# --------------------------------------------------------------------------- #
FROM base AS runtime
USER 1000:1000

# The user, the ownership and the directories were all settled in `base`.
VOLUME ["/data"]

EXPOSE 8000
# Docker's own view of whether the app answers, for compose and Unraid; on
# Kubernetes the chart's probes do this. /healthz needs no database, and the
# image has no curl, so Python asks it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
# config.yaml is mounted from a ConfigMap at /config/config.yaml in-cluster;
# on compose/Unraid the wizard writes it there itself on first run.
# Migrations run at startup (appcore.upgrade_to_head) before serving.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
