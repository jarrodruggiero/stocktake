# CI

*(This file is `CI.md`, not `README.md`, on purpose: GitHub shows
`.github/README.md` on the repository landing page in preference to the root
one, so a README here hides the project's own.)*

Everything here runs on GitHub-hosted runners with public actions and the token
GitHub issues to the job. There are no secrets to provision — `GITHUB_TOKEN`
covers the image push to ghcr.io.

| File | Does |
| --- | --- |
| `workflows/ci.yml` | ruff, then pytest on SQLite **and** Postgres, then build the image and run the suite inside it |
| `workflows/release.yml` | on a `v*` tag: re-verify, then push a multi-arch (amd64 + arm64) image to ghcr.io and open a Release |
| `workflows/docs.yml` | publish `docs/` to GitHub Pages |
| `dependabot.yml` | weekly pip updates (grouped), monthly actions and Docker |

## Python versions — tested, not assumed

`requires-python = ">=3.12"`, and CI tests **every version that claim covers**:
3.12, 3.13 and 3.14. If one ever has to be dropped, narrow `requires-python` —
do not quietly stop testing it.

Postgres runs on 3.12 only. The interpreter and the backend are independent
axes, so the full cross product would be six jobs to learn what four already
tell us.

## Two things that look like decoration and are not

**`known-first-party` is set explicitly in `pyproject.toml`.** Ruff otherwise
infers first-party packages from the layout, and `appkit` is a directory at the
root here rather than an installed package. Inference that depends on layout
sorts the same file differently depending on where it is checked out, which
fails CI for a reason nobody can reproduce locally.

**The image is built on every pull request**, not only at release. A Dockerfile
that stopped working is otherwise discovered at the moment you most want a
release, and its `test` stage runs the suite against the layers the app actually
ships from — including OCR, which skips on a runner without Tesseract.
