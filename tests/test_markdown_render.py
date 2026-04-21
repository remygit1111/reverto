"""Tests for core.markdown_render — changelog body sanitisation.

Markdown is rendered by markdown-it-py and then cleaned by bleach
with an explicit tag/attribute whitelist. Anything that looks like an
XSS vector — ``<script>``, inline ``style``, ``onerror`` handlers,
``javascript:`` URLs — must be stripped before the HTML reaches the
browser.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.markdown_render import render_markdown


class TestBasicMarkdown:

    def test_bold_renders(self):
        out = render_markdown("This is **bold** text.")
        assert "<strong>bold</strong>" in out

    def test_italic_renders(self):
        out = render_markdown("This is *italic* text.")
        assert "<em>italic</em>" in out

    def test_unordered_list_renders(self):
        out = render_markdown("- one\n- two\n- three")
        assert "<ul>" in out
        assert "<li>one</li>" in out

    def test_ordered_list_renders(self):
        out = render_markdown("1. first\n2. second")
        assert "<ol>" in out
        assert "<li>first</li>" in out

    def test_code_block_renders(self):
        out = render_markdown("```\nsome code\n```")
        assert "<pre>" in out
        assert "<code>" in out

    def test_inline_code_renders(self):
        out = render_markdown("Use the `get()` helper.")
        assert "<code>get()</code>" in out

    def test_link_renders_with_rel_attrs(self):
        out = render_markdown("Visit [example](https://example.com).")
        assert 'href="https://example.com"' in out
        # Bleach linkifier adds rel + target for safety.
        assert "noopener" in out or "noreferrer" in out

    def test_blockquote_renders(self):
        out = render_markdown("> quote me")
        assert "<blockquote>" in out

    def test_empty_input_returns_empty_string(self):
        assert render_markdown("") == ""
        assert render_markdown("   \n  ") == ""


class TestSanitisation:

    def test_script_tag_is_stripped(self):
        """markdown-it runs with html=False so raw HTML is escaped into
        text before bleach ever sees it — the <script> opening tag
        never becomes an element, the payload renders as harmless
        entity-encoded text. Assert that no live <script> element
        survives; the escaped literal is fine."""
        malicious = "before <script>alert('xss')</script> after"
        out = render_markdown(malicious)
        # No executable script element.
        assert "<script>" not in out.lower()
        assert "<script " not in out.lower()
        # Content that didn't live in the tag survives.
        assert "before" in out
        assert "after" in out

    def test_inline_style_attribute_is_stripped(self):
        """Raw <p style="..."> is entity-escaped into harmless text by
        markdown-it (html=False). Bleach also lists ``style`` as a
        disallowed attribute, so even if a future config change lets
        raw HTML through the attribute still goes. Assert that no
        live element carries a style attribute."""
        out = render_markdown('<p style="color:red">styled</p>')
        # No live element — input was escaped to text.
        assert "<p style" not in out
        # And no live attribute landed on any tag the renderer did emit.
        assert 'style="' not in out

    def test_iframe_is_stripped(self):
        """No live <iframe> element. The URL may still appear as
        entity-escaped text — that's display-only and doesn't fetch
        anything, so we only assert no executable frame survives."""
        out = render_markdown('<iframe src="https://evil.example/"></iframe>')
        assert "<iframe" not in out.lower()

    def test_onerror_handler_is_stripped(self):
        """<img> isn't in the allow-list AND markdown-it html=False
        strips raw HTML — nothing executable survives. The raw
        attribute text may remain as escaped characters; we only
        assert no live element carries it."""
        out = render_markdown('<img src=x onerror="alert(1)">')
        assert "<img" not in out.lower()
        # Escaped text form: "&lt;img src=x onerror=..." is harmless.
        # Assert no live <... onerror="..."> attribute survives on any
        # tag the renderer actually emits.
        import re
        live_onerror = re.search(r'<[a-zA-Z]+[^>]*onerror=', out)
        assert live_onerror is None

    def test_javascript_url_is_stripped(self):
        """A markdown link whose target is javascript:... must not
        emit an executable <a href="javascript:..."> element. markdown-it
        rejects the disallowed protocol and leaves the text as plain
        content; bleach also refuses to pass the protocol if it
        somehow reached the HTML stage."""
        out = render_markdown("[click](javascript:alert(1))")
        # No live link carries a javascript: target.
        assert 'href="javascript:' not in out.lower()
        assert "href='javascript:" not in out.lower()

    def test_data_url_is_stripped(self):
        """Same as above for data: URLs — a data: target would let an
        attacker ship a self-contained HTML payload to the click."""
        out = render_markdown("[click](data:text/html,<script>alert(1)</script>)")
        assert 'href="data:' not in out.lower()
        assert "href='data:" not in out.lower()

    def test_raw_html_form_is_stripped(self):
        """markdown-it is configured with html=False, so raw HTML is
        escaped into text. No live <form> element must survive —
        otherwise a changelog entry could render an in-page form that
        targets an authenticated endpoint."""
        out = render_markdown('<form action="/admin/reset">Reset</form>')
        assert "<form" not in out.lower()
        assert 'action="/admin/reset"' not in out


class TestSafeHtmlPassthrough:
    """Tags that ARE in the allow-list must round-trip when generated
    by the markdown renderer. If bleach widens the whitelist in a
    future bump, these tests catch a misconfiguration that strips
    safe content."""

    def test_headings_survive(self):
        out = render_markdown("# Title\n\n## Subtitle")
        assert "<h1>Title</h1>" in out
        assert "<h2>Subtitle</h2>" in out

    def test_horizontal_rule_survives(self):
        out = render_markdown("before\n\n---\n\nafter")
        assert "<hr" in out
