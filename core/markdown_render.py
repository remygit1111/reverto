"""Markdown → sanitised HTML.

Used by the /changelog page to render entry descriptions. The input
is trusted only insofar as it was typed by an admin — but admins can
make mistakes (copy-paste an email template with an inline-style
``<img onerror=...>`` gadget), so the output still runs through
bleach with an explicit whitelist. No raw HTML, no ``<script>``, no
``style`` / ``onclick`` attributes, no ``<iframe>`` / ``<object>`` /
``<embed>``.

Library choices:
  * ``markdown-it-py`` for parsing — commonmark-compliant, pure
    Python, zero runtime surprises.
  * ``bleach`` for sanitisation — the Django-community standard;
    works with an explicit allow-list that matches what the changelog
    UI can style.

Both are pinned in ``requirements.txt``.
"""

from __future__ import annotations

import bleach
from markdown_it import MarkdownIt

_ALLOWED_TAGS: frozenset[str] = frozenset({
    "p",
    "br",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "b", "i",
    "ul", "ol", "li",
    "a",
    "code", "pre",
    "blockquote",
    "hr",
})

_ALLOWED_ATTRIBUTES: dict[str, list[str]] = {
    "a": ["href", "title", "rel"],
}

# ``http`` / ``https`` / ``mailto`` cover the useful links users might
# drop in; ``javascript:`` / ``data:`` / ``file:`` are deliberately
# excluded so bleach strips link targets that try to execute code.
_ALLOWED_PROTOCOLS: frozenset[str] = frozenset({"http", "https", "mailto"})

# markdown-it-py default preset. ``html = False`` disables inline HTML
# passthrough, which is the first line of defence against XSS; bleach
# is the second line. ``linkify = True`` auto-converts bare URLs into
# ``<a>`` tags so the rendered output is consistent whether the admin
# typed a full markdown link or pasted a naked URL.
_MD = MarkdownIt("commonmark", {"html": False, "linkify": True, "breaks": False})


def render_markdown(text: str) -> str:
    """Render a markdown string to sanitised HTML.

    Empty / whitespace input returns the empty string (renderers don't
    like to emit ``<p></p>`` for blank inputs, and the empty-state UI
    is cleaner without it).
    """
    if not text or not text.strip():
        return ""
    html = _MD.render(text)
    cleaned = bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
    # bleach-linkify every remaining bare URL (belt-and-braces — the
    # renderer emits <a> tags for naked URLs too, but the sanitiser
    # run may have stripped something unexpected).
    cleaned = bleach.linkify(
        cleaned,
        callbacks=[
            lambda attrs, new=False: {
                **attrs,
                (None, "rel"): "noopener noreferrer nofollow",
                (None, "target"): "_blank",
            },
        ],
        skip_tags=["pre", "code"],
    )
    return cleaned
