"""Pictures in the transcript: where a model-written image path points, and
which attachments of a user message are pictures.

A model shows a picture it made with ordinary markdown,
`![a dog](generated_images/dog_s7.png)`. The path is relative to the
workspace, but an `<img>` resolves it against the page, so before this change
the picture loaded only when the model happened to write the `/workspace/`
prefix, and then at its natural size. `render.js` now maps a workspace image
to its real URL and turns it into a thumbnail that opens in a viewer.

`workspaceImageSrc` is the gate for that, so it is also where the limits are:
nothing with a scheme, nothing outside the workspace, nothing that is not a
raster image. These tests run the real function bytes under node.
"""

from __future__ import annotations

import pytest

from tests.js_harness import requires_node, run_js

pytestmark = requires_node


SCENARIO = r"""
import { decls, fns, makeContext, mod, run, report } from './sandbox.mjs';

const RENDER = mod('render.js');
const CODE = [
  decls(['CHAT_IMAGE_EXT', 'ATTACHED_REF'], RENDER),
  fns(['workspaceImageSrc', 'attachedImageSrcs'], RENDER),
].join('\n\n');

const { ctx } = makeContext({ console });
run(ctx, CODE.replace(/^export /gm, ''));
const src = s => { ctx.__s = s; return run(ctx, 'workspaceImageSrc(__s)'); };
const att = s => { ctx.__s = s; return run(ctx, 'JSON.stringify(attachedImageSrcs(__s))'); };

report({
  relative: src('generated_images/dog_s7.png'),
  dotSlash: src('./generated_images/dog_s7.png'),
  prefixed: src('/workspace/generated_images/dog_s7.png'),
  barePrefix: src('workspace/generated_images/dog_s7.png'),
  container: src('/app/data/workspace/generated_images/dog_s7.png'),
  root: src('photo.JPG'),
  spaces: src('bin_images/Pasted image.png'),
  encoded: src('bin_images/Pasted%20image.png'),
  query: src('/workspace/a.png?v=2#x'),
  webp: src('a/b/c.webp'),
  http: src('https://example.com/a.png'),
  protoRel: src('//example.com/a.png'),
  dataUrl: src('data:image/png;base64,AAAA'),
  js: src('javascript:alert(1)//.png'),
  traversal: src('../secrets/a.png'),
  traversalDeep: src('/workspace/a/../../b.png'),
  traversalEncoded: src('a/%2e%2e/b.png'),
  encodedSlash: src('a/..%2fb.png'),
  staticPath: src('/static/img/favicon.png'),
  apiPath: src('/api/workspace/a.png'),
  svg: src('diagram.svg'),
  notImage: src('report.md'),
  empty: src(''),
  notString: src(null),
  attached: JSON.parse(att('add a dog\n\n[attached: beach.jpg] [attached: notes.pdf] [attached: IMG 2616.png]')),
  attachedRepeat: JSON.parse(att('[attached: a.png] [attached: a.png]')),
  attachedAnnotated: JSON.parse(att('[attached: talk.pdf — text at talk.pdf.txt]')),
  attachedNone: JSON.parse(att('no files here')),
});
"""


@pytest.fixture(scope="module")
def img(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("chatimg"))


@pytest.mark.parametrize("case", ["relative", "dotSlash", "prefixed", "barePrefix", "container"])
def test_every_spelling_of_a_workspace_path_lands_on_the_same_url(img, case):
    """The model is told to write workspace-relative paths, and writes all of
    these. Each must load, not just the one with the prefix."""
    assert img[case] == "/workspace/generated_images/dog_s7.png"


def test_a_file_in_the_workspace_root_and_other_raster_types(img):
    assert img["root"] == "/workspace/photo.JPG"
    assert img["webp"] == "/workspace/a/b/c.webp"
    assert img["query"] == "/workspace/a.png"


def test_a_name_with_a_space_is_encoded_once(img):
    """marked percent-encodes the space, a hand-written path does not. Both
    must come out single-encoded or the file is not found."""
    assert img["spaces"] == "/workspace/bin_images/Pasted%20image.png"
    assert img["encoded"] == "/workspace/bin_images/Pasted%20image.png"


@pytest.mark.parametrize("case", ["http", "protoRel", "dataUrl", "js"])
def test_nothing_with_a_scheme_or_another_host_is_touched(img, case):
    """A remote image stays what the sanitizer returned (and the page CSP
    refuses to load it). It must never be rewritten into something that loads."""
    assert img[case] is None


@pytest.mark.parametrize("case", ["traversal", "traversalDeep", "traversalEncoded", "encodedSlash"])
def test_a_path_cannot_climb_out_of_the_workspace(img, case):
    assert img[case] is None


@pytest.mark.parametrize("case", ["staticPath", "apiPath"])
def test_other_absolute_paths_of_the_app_are_left_alone(img, case):
    assert img[case] is None


@pytest.mark.parametrize("case", ["svg", "notImage", "empty", "notString"])
def test_only_raster_images_become_thumbnails(img, case):
    """SVG is excluded on purpose: it is a scripting context."""
    assert img[case] is None


def test_the_pictures_attached_to_a_user_message(img):
    assert img["attached"] == ["/workspace/beach.jpg", "/workspace/IMG%202616.png"]
    assert img["attachedRepeat"] == ["/workspace/a.png"]
    assert img["attachedAnnotated"] == []
    assert img["attachedNone"] == []
