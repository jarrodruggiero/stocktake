"""Rules about the image the app ships in, read from the Dockerfile.

The base was pinned by digest alone, so Dependabot followed `latest`, which
is the full Python image: 415 MB compressed against 43 MB, with compilers,
git and curl in what ships. It went from slim to full three times, each
through a pull request titled with nothing but two short digests. And with
compilers in the test stage, a dependency that quietly started compiling
from source still passed.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")
# Every image the build pulls: `FROM image` and `COPY --from=image`.
PULLED = re.findall(r"^(?:FROM|COPY --from=)\s*(\S+)", DOCKERFILE, re.M)
IMAGES = [ref for ref in PULLED if "/" in ref or "@" in ref or ":" in ref]


def test_the_python_image_is_slim():
    python = [ref for ref in IMAGES if "/python" in ref]

    assert python
    assert all(re.search(r"/python:3\.\d+-slim@sha256:[0-9a-f]{64}$", ref) for ref in python), python


def test_every_pinned_image_names_the_tag_it_follows():
    """A digest alone gives Dependabot nothing to follow but `latest`. The tag
    beside it is what an update keeps to, and what a reviewer reads."""
    pinned = [ref for ref in IMAGES if "@sha256:" in ref]

    assert pinned
    assert [ref for ref in pinned if not re.search(r":[\w.-]+@sha256:", ref)] == []


def test_every_image_is_on_a_from_line():
    """Dependabot reads FROM lines and nothing else. uv was copied straight
    out of its image, so it was never proposed an update and stayed at the
    version of the first commit."""
    copied = re.findall(r"^COPY --from=(\S+)", DOCKERFILE, re.M)
    stages = re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", DOCKERFILE, re.M)

    assert copied
    assert [name for name in copied if name not in stages] == []


def _instructions(text: str) -> str:
    """The Dockerfile without its comments."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


SHIPPED = _instructions(DOCKERFILE[DOCKERFILE.index(" AS base"):])


def test_git_and_uv_stay_in_the_build_stages():
    """Needed to make the virtualenv, not to run the app: git fetches appcore,
    uv installs from the lock. Neither reaches base, test or runtime."""
    assert "/uv" not in SHIPPED and "uv sync" not in SHIPPED
    assert not re.search(r"apt-get install[^\n]*\bgit\b", SHIPPED)


def test_the_app_cannot_rewrite_its_own_code():
    """The code and the virtualenv were the runtime user's, so the process
    could change them. It owns what it writes, /data and /config, and no
    more; and never by a recursive chown, which once doubled the image. The
    test stage, which never ships, may give the suite its own files."""
    base = DOCKERFILE[DOCKERFILE.index(" AS base"):DOCKERFILE.index(" AS test")]
    instructions = _instructions(base + DOCKERFILE[DOCKERFILE.index(" AS runtime"):])

    assert "--chown" not in instructions
    assert "chown -R" not in instructions
    assert re.search(r"chown 1000:1000 /data /config\b", instructions)


def test_the_running_image_says_whether_it_is_healthy():
    runtime = _instructions(DOCKERFILE[DOCKERFILE.index(" AS runtime"):])

    assert re.search(r"^HEALTHCHECK .*\n\s+CMD .*/healthz", runtime, re.M)
    assert re.search(r"^USER 1000:1000$", runtime, re.M)


def test_the_build_is_not_sent_a_virtualenv():
    ignored = (Path(__file__).resolve().parent.parent / ".dockerignore").read_text(
        encoding="utf-8").split()

    assert {".venv", ".git", "**/__pycache__"} <= set(ignored)


def test_every_builder_image_a_workflow_uses_is_pinned():
    """`binfmt:latest` and `buildkit:buildx-stable-1` floated: the image that
    builds the image changed underneath every release."""
    workflows = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    images = [match for path in sorted(workflows.glob("*.yml"))
              for match in re.findall(r"image[:=]\s*(\S*(?:binfmt|buildkit)\S*)",
                                      path.read_text(encoding="utf-8"))]

    assert len(images) == 5
    assert [ref for ref in images if not re.search(r":[\w.-]+@sha256:[0-9a-f]{64}$", ref)] == []
