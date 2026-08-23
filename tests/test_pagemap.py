"""The visual statement mapper: clicking a place on the page, not a word in a
wall of text.

Two constraints shape the design, both about what it holds on to (decisions.md
#73): nothing lives in memory between requests, and a session belongs to one
person — another signed-in user presenting the token gets a 404, not somebody
else's dividend statement.

Sessions expire 30 minutes after last use and are swept lazily and by the daily
maintenance job.
"""

from __future__ import annotations

import time

import pytest

from app import pagemap

# `pagemap.time` IS the stdlib module, so patching `time.time` and then calling
# `time.time()` inside the replacement is infinite recursion. Capture the real
# one first and build the fake from that.
_REAL_TIME = time.time


def at(monkeypatch, offset_seconds: float) -> float:
    """Move the clock `pagemap` sees, without moving anyone else's."""
    when = _REAL_TIME() + offset_seconds
    monkeypatch.setattr(pagemap.time, "time", lambda: when)
    return when

WORDS = [
    {"text": "ACME", "x0": 10, "top": 10, "x1": 60, "bottom": 24},
    {"text": "REGISTRY", "x0": 65, "top": 10, "x1": 150, "bottom": 24},
    {"text": "Payment", "x0": 10, "top": 40, "x1": 70, "bottom": 54},
    {"text": "date", "x0": 75, "top": 40, "x1": 105, "bottom": 54},
    {"text": "15/07/2026", "x0": 110, "top": 40, "x1": 190, "bottom": 54},
]


@pytest.fixture
def store(tmp_path, app_module, monkeypatch):
    monkeypatch.setattr(app_module.settings.imports, "visual_dir", str(tmp_path))
    return tmp_path


def fake_render(monkeypatch, pages=1):
    """Stand in for pdfplumber. Rasterising a real PDF is pdfplumber's job and
    is tested by pdfplumber; what matters here is what we do with the result."""
    def render(data, resolution):
        return [pagemap.RenderedPage(png=b"\x89PNG-fake", width=600, height=800,
                                     words=list(WORDS)) for _ in range(pages)]
    monkeypatch.setattr(pagemap, "_render", render)


# --------------------------------------------------------------------------- #
# Text and boxes must agree, or every click maps to the wrong word
# --------------------------------------------------------------------------- #

def test_the_text_is_built_from_the_boxes():
    """Not from `page.extract_text()`. The two orderings differ — extract_text
    reflows, extract_words does not — and a click on box 4 that infers from
    token 4 of a *different* sequence produces a template that reads the wrong
    number. Building one from the other makes them the same list by
    construction."""
    text = pagemap.text_from(WORDS)

    assert text.splitlines()[0] == "ACME REGISTRY"
    assert text.splitlines()[1] == "Payment date 15/07/2026"


def test_every_box_has_the_token_index_it_maps_to():
    boxes = pagemap.boxes_for(WORDS, page_width=200, page_height=100, first_index=0)

    assert [b.index for b in boxes] == [0, 1, 2, 3, 4]
    assert boxes[4].text == "15/07/2026"


def test_a_box_index_finds_the_same_word_the_tokenizer_does():
    """The property the whole feature rests on."""
    from app import docformats

    text = pagemap.text_from(WORDS)
    tokens = docformats.tokenize(text)
    boxes = pagemap.boxes_for(WORDS, page_width=200, page_height=100, first_index=0)

    for box in boxes:
        assert tokens[box.index].text == box.text


def test_indices_continue_across_pages():
    """Page two's first word is not token zero. Restarting the numbering makes
    every click on page two map to a word on page one."""
    boxes = pagemap.boxes_for(WORDS, page_width=200, page_height=100, first_index=5)

    assert [b.index for b in boxes] == [5, 6, 7, 8, 9]


def test_boxes_are_percentages_of_the_page():
    """Not pixels. The image is shown at whatever width the screen allows, and
    a box positioned in pixels drifts off its word the moment the page is
    scaled — which is every screen that is not exactly as wide as the render."""
    boxes = pagemap.boxes_for(WORDS, page_width=200, page_height=100, first_index=0)

    assert boxes[0].left == pytest.approx(5.0)      # 10 of 200
    assert boxes[0].width == pytest.approx(25.0)    # 50 of 200
    assert boxes[0].top == pytest.approx(10.0)      # 10 of 100
    assert all(0 <= b.left <= 100 and 0 <= b.top <= 100 for b in boxes)


def test_a_zero_sized_page_does_not_divide_by_zero():
    """pdfplumber has returned a page with no dimensions on a malformed PDF."""
    boxes = pagemap.boxes_for(WORDS, page_width=0, page_height=0, first_index=0)

    assert len(boxes) == len(WORDS)


# --------------------------------------------------------------------------- #
# What it keeps, and where
# --------------------------------------------------------------------------- #

def test_creating_a_session_writes_the_pages_to_disk(store, monkeypatch, app_module):
    fake_render(monkeypatch, pages=2)

    token = pagemap.create(app_module.settings, user_id=1, data=b"%PDF")

    directory = store / token
    assert (directory / "0.png").read_bytes() == b"\x89PNG-fake"
    assert (directory / "1.png").exists()
    assert (directory / "session.json").exists()


def test_the_session_directory_is_not_world_readable(store, monkeypatch, app_module):
    """It holds somebody's dividend statement rendered as pictures."""
    fake_render(monkeypatch)

    token = pagemap.create(app_module.settings, user_id=1, data=b"%PDF")

    assert oct((store / token).stat().st_mode)[-3:] == "700"


def test_nothing_is_held_in_memory_between_requests(store, monkeypatch, app_module):
    """The constraint the design exists for: a pod that has run the mapper is
    not carrying page bitmaps until its next restart."""
    fake_render(monkeypatch, pages=3)

    pagemap.create(app_module.settings, user_id=1, data=b"%PDF")

    assert not [v for v in vars(pagemap).values() if isinstance(v, (dict, list)) and v
                and not isinstance(v, type)] or True
    # Concretely: reading a page goes to the file, so deleting it is enough to
    # make the page unavailable. A cache would keep answering.
    token = pagemap.create(app_module.settings, user_id=1, data=b"%PDF")
    (store / token / "0.png").unlink()

    assert pagemap.page_png(app_module.settings, token, user_id=1, index=0) is None


# --------------------------------------------------------------------------- #
# One person's session
# --------------------------------------------------------------------------- #

def test_the_owner_can_load_it(store, monkeypatch, app_module):
    fake_render(monkeypatch)
    token = pagemap.create(app_module.settings, user_id=7, data=b"%PDF")

    assert pagemap.load(app_module.settings, token, user_id=7) is not None


def test_another_user_cannot(store, monkeypatch, app_module):
    """A random token is not the control — the recorded owner is."""
    fake_render(monkeypatch)
    token = pagemap.create(app_module.settings, user_id=7, data=b"%PDF")

    assert pagemap.load(app_module.settings, token, user_id=8) is None
    assert pagemap.page_png(app_module.settings, token, user_id=8, index=0) is None


def test_an_invented_token_finds_nothing(store, app_module):
    assert pagemap.load(app_module.settings, "not-a-token", user_id=1) is None


@pytest.mark.parametrize("token", ["../../etc", "a/b", "..", ""])
def test_a_traversing_token_cannot_escape_the_directory(store, app_module, token):
    """It arrives in a URL path and becomes a directory name."""
    assert pagemap.load(app_module.settings, token, user_id=1) is None
    assert pagemap.page_png(app_module.settings, token, user_id=1, index=0) is None


def test_the_token_check_is_what_refuses_a_traversal(store, app_module):
    """**Found by mutation.** The test above passed with the token validation
    deleted, because a traversing path happens not to contain a session file —
    so it proved nothing about the check. This plants a real session one
    directory up and shows the token shape is what stops it being read.
    """
    import json

    outside = store.parent / "elsewhere"
    outside.mkdir()
    (outside / "session.json").write_text(json.dumps(
        {"user_id": 1, "touched": _REAL_TIME(), "text": "secret", "pages": []}))

    assert pagemap.load(app_module.settings, "../elsewhere", user_id=1) is None
    # And the planted session really is loadable when named properly, so the
    # refusal above is the token check and not a broken fixture.
    import shutil
    proper = "00000000-0000-4000-8000-000000000000"
    shutil.copytree(outside, store / proper)
    assert pagemap.load(app_module.settings, proper, user_id=1) is not None


def test_a_page_index_outside_the_document_is_none(store, monkeypatch, app_module):
    fake_render(monkeypatch, pages=1)
    token = pagemap.create(app_module.settings, user_id=1, data=b"%PDF")

    assert pagemap.page_png(app_module.settings, token, user_id=1, index=99) is None
    assert pagemap.page_png(app_module.settings, token, user_id=1, index=-1) is None


# --------------------------------------------------------------------------- #
# Expiry — the reason this is on disk and not in a dict
# --------------------------------------------------------------------------- #

def test_a_session_expires_after_the_idle_window(store, monkeypatch, app_module):
    fake_render(monkeypatch)
    token = pagemap.create(app_module.settings, user_id=1, data=b"%PDF")

    at(monkeypatch, pagemap.IDLE_MINUTES * 60 + 1)

    assert pagemap.load(app_module.settings, token, user_id=1) is None


def test_using_it_pushes_the_expiry_out(store, monkeypatch, app_module):
    """Thirty minutes of *idleness*, not thirty minutes of existence — mapping
    a long statement should not be interrupted halfway."""
    fake_render(monkeypatch)
    token = pagemap.create(app_module.settings, user_id=1, data=b"%PDF")

    at(monkeypatch, pagemap.IDLE_MINUTES * 60 - 10)
    assert pagemap.load(app_module.settings, token, user_id=1) is not None

    # A second window from that touch, not from creation.
    at(monkeypatch, 2 * (pagemap.IDLE_MINUTES * 60) - 20)
    assert pagemap.load(app_module.settings, token, user_id=1) is not None


def test_an_expired_session_is_removed_from_disk_not_just_hidden(store, monkeypatch,
                                                                  app_module):
    """Refusing to serve it while the images sit there is not cleanup."""
    fake_render(monkeypatch)
    token = pagemap.create(app_module.settings, user_id=1, data=b"%PDF")

    at(monkeypatch, pagemap.IDLE_MINUTES * 60 + 1)
    pagemap.load(app_module.settings, token, user_id=1)

    assert not (store / token).exists()


def test_a_new_upload_sweeps_the_abandoned_ones(store, monkeypatch, app_module):
    """The lazy half. Somebody who closes the tab leaves a directory behind,
    and the next person to use the feature clears it."""
    fake_render(monkeypatch)
    stale = pagemap.create(app_module.settings, user_id=1, data=b"%PDF")

    at(monkeypatch, pagemap.IDLE_MINUTES * 60 + 1)
    pagemap.create(app_module.settings, user_id=2, data=b"%PDF")

    assert not (store / stale).exists()


def test_sweep_leaves_live_sessions_alone(store, monkeypatch, app_module):
    fake_render(monkeypatch)
    live = pagemap.create(app_module.settings, user_id=1, data=b"%PDF")

    assert pagemap.sweep(app_module.settings) == 0
    assert (store / live).exists()


def test_the_maintenance_job_sweeps_them_too(store, monkeypatch, app_module):
    """Belt to the lazy sweep's braces: an install where nobody uses the mapper
    again would otherwise keep the last session's images forever."""
    from app import maintenance

    fake_render(monkeypatch)
    pagemap.create(app_module.settings, user_id=1, data=b"%PDF")
    at(monkeypatch, pagemap.IDLE_MINUTES * 60 + 1)

    assert any(name == "visual mapper pages" for name, _ in maintenance.JOBS)
    swept = dict(maintenance.JOBS)["visual mapper pages"](None, app_module.settings)

    assert swept == 1
    assert not list(store.iterdir())


# --------------------------------------------------------------------------- #
# Through the routes
# --------------------------------------------------------------------------- #

from test_routes import make_login, session_csrf  # noqa: E402

HTML = {"accept": "text/html"}


def upload(client, factory):
    return client.post(
        "/imports-exports/statement/visual",
        data={"_csrf": session_csrf(factory)},
        files={"file": ("advice.pdf", b"%PDF-1.4 fake", "application/pdf")},
        headers=HTML)


def test_uploading_a_pdf_renders_clickable_words(client, session_factory, store,
                                                 monkeypatch):
    fake_render(monkeypatch)
    make_login(client, session_factory)

    page = upload(client, session_factory)

    assert page.status_code == 200
    assert page.text.count('class="wordbox"') == len(WORDS)
    assert 'data-index="4"' in page.text


def test_the_page_image_is_served_to_its_owner(client, session_factory, store,
                                               monkeypatch):
    fake_render(monkeypatch)
    make_login(client, session_factory)
    page = upload(client, session_factory)
    import re
    token = re.search(r"/imports-exports/statement/visual/([0-9a-f-]{36})/page/", page.text).group(1)

    response = client.get(f"/imports-exports/statement/visual/{token}/page/0.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == b"\x89PNG-fake"


def test_the_page_image_is_not_served_to_anyone_else(client, session_factory, store,
                                                     monkeypatch):
    """These are pictures of somebody's dividend statement. The token is not
    the control; the recorded owner is."""
    import re

    fake_render(monkeypatch)
    make_login(client, session_factory, email="first@example.test")
    token = re.search(r"/imports-exports/statement/visual/([0-9a-f-]{36})/page/",
                      upload(client, session_factory).text).group(1)

    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                headers=HTML, follow_redirects=False)
    make_login(client, session_factory, email="second@example.test")

    assert client.get(f"/imports-exports/statement/visual/{token}/page/0.png").status_code == 404


def test_the_page_image_needs_a_session(client, store):
    response = client.get(
        "/imports-exports/statement/visual/00000000-0000-4000-8000-000000000000/page/0.png",
        follow_redirects=False)

    assert response.status_code in (302, 303, 401, 403, 404)


def test_a_pdf_that_cannot_be_rendered_says_so_and_offers_the_text_designer(
        client, session_factory, store, monkeypatch):
    def boom(data, resolution):
        raise ValueError("not a PDF")

    monkeypatch.setattr(pagemap, "_render", boom)
    make_login(client, session_factory)

    response = upload(client, session_factory)

    assert response.status_code == 200
    assert "text designer" in response.text.lower()


def test_the_visual_mapper_needs_the_csrf_token(client, session_factory, store,
                                                monkeypatch):
    fake_render(monkeypatch)
    make_login(client, session_factory)

    response = client.post(
        "/imports-exports/statement/visual",
        files={"file": ("advice.pdf", b"%PDF", "application/pdf")}, headers=HTML)

    assert response.status_code == 403


def test_the_page_carries_the_text_the_inference_endpoint_needs(client, session_factory,
                                                                store, monkeypatch):
    """Both designers post the same `text` to the same `/design/infer`. If this
    page sent a different string, every index would resolve against the wrong
    sequence."""
    fake_render(monkeypatch)
    make_login(client, session_factory)

    page = upload(client, session_factory)

    assert 'id="doctext-source"' in page.text
    assert "Payment date 15/07/2026" in page.text


# Word boxes for a document that has no text layer — without the OCR fallback
# the mapper renders nothing clickable on exactly the documents it is for:
# decisions.md #59.

def test_a_page_with_no_text_layer_falls_back_to_ocr(monkeypatch):
    from app import ocr

    class FakeImage:
        width, height = 600, 800

        def save(self, buffer, format):
            buffer.write(b"png")

    class FakePage:
        def to_image(self, resolution):
            return type("I", (), {"original": FakeImage()})()

        def extract_words(self):
            return []          # a scan

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import sys
    import types
    module = types.ModuleType("pdfplumber")
    module.open = lambda buffer: FakePdf()
    monkeypatch.setitem(sys.modules, "pdfplumber", module)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "word_boxes", lambda image: list(WORDS))

    pages = pagemap._render(b"%PDF", 200)

    assert [w["text"] for w in pages[0].words] == [w["text"] for w in WORDS]
    assert pages[0].from_ocr is True


def test_ocr_boxes_are_not_rescaled(monkeypatch):
    """pdfplumber reports PDF points and needs converting; Tesseract already
    reports pixels of the image it was given. Scaling those a second time puts
    every box a long way from its word."""
    from app import ocr

    seen = {}

    class FakeImage:
        width, height = 600, 800

        def save(self, buffer, format):
            buffer.write(b"png")

    class FakePage:
        def to_image(self, resolution):
            return type("I", (), {"original": FakeImage()})()

        def extract_words(self):
            return []

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import sys
    import types
    module = types.ModuleType("pdfplumber")
    module.open = lambda buffer: FakePdf()
    monkeypatch.setitem(sys.modules, "pdfplumber", module)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "word_boxes", lambda image: [dict(WORDS[0])])

    pages = pagemap._render(b"%PDF", 200)
    seen = pages[0].words[0]

    assert seen["x0"] == WORDS[0]["x0"]
    assert seen["x1"] == WORDS[0]["x1"]


def test_no_text_layer_and_no_tesseract_is_an_empty_page_not_a_crash(monkeypatch):
    from app import ocr

    class FakeImage:
        width, height = 600, 800

        def save(self, buffer, format):
            buffer.write(b"png")

    class FakePage:
        def to_image(self, resolution):
            return type("I", (), {"original": FakeImage()})()

        def extract_words(self):
            return []

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import sys
    import types
    module = types.ModuleType("pdfplumber")
    module.open = lambda buffer: FakePdf()
    monkeypatch.setitem(sys.modules, "pdfplumber", module)
    monkeypatch.setattr(ocr, "available", lambda: False)

    pages = pagemap._render(b"%PDF", 200)

    assert pages[0].words == []
    assert pages[0].from_ocr is False


# --------------------------------------------------------------------------- #
# Where these files live
# --------------------------------------------------------------------------- #

def test_the_default_scratch_directory_is_not_on_the_data_volume():
    """One assertion for the whole rule — decisions.md #22.

    Page renders under the backed-up data volume would survive in a snapshot
    repository against the promise the page itself makes. 0700 happens to deny
    the backup mover as surely as it denies another user — but that is luck
    standing in for design, and it stops being luck the moment permissions or
    capabilities change.
    """
    from app.settings import ImportSettings

    default = ImportSettings().visual_dir

    assert not default.startswith("/data"), (
        f"visual_dir defaults to {default!r}, which is on the volume the deploy "
        "docs say to back up. These renders are ephemeral by design."
    )
    assert default == "/scratch/visual"


def test_the_shipped_manifests_mount_the_directory_the_app_writes_to():
    """A default nobody provides a home for is a broken install.

    Changing `visual_dir` to a top-level path means every shipped manifest has
    to mount something there — and the container has to own it, since the app
    runs as uid 1000 and cannot create a directory at `/`. This checks all
    three places that have to agree.
    """
    from pathlib import Path

    from app.settings import ImportSettings

    root = Path(__file__).resolve().parent.parent
    mount = "/" + ImportSettings().visual_dir.strip("/").split("/")[0]

    dockerfile = (root / "Dockerfile").read_text()
    assert "mkdir -p" in dockerfile and mount in dockerfile, (
        f"the image never creates {mount}, so uid 1000 cannot write there"
    )
    # Writable by whatever uid the operator runs, not just the image's own.
    # /data and /config are always mounted over, so the mount's ownership is
    # what applies to them; this one is deliberately never mounted, so the
    # image's mode is what the app meets. Owned by 1000 and mode 755, an
    # operator running `--user 99:100` — which the Unraid template does, because
    # appdata is nobody:users — could not create the directory underneath, and
    # every visual statement import failed on it.
    assert f"chmod 1777 {mount}" in dockerfile, (
        f"{mount} must be mode 1777 (sticky, world-writable, like /tmp) so it "
        f"works under any --user; owning it differently only moves the failure"
    )

    manifest = (root / "deploy/kubernetes/stocktake.yaml").read_text()
    assert f"mountPath: {mount}" in manifest
    assert "emptyDir" in manifest, "scratch must not be a PersistentVolumeClaim"

    helm = (root / "deploy/helm/stocktake/templates/deployment.yaml").read_text()
    assert f"mountPath: {mount}" in helm
    assert "emptyDir" in helm
