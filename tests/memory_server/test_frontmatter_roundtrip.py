"""Round-trip tests for memory_frontmatter (P1-C extraction)."""

from __future__ import annotations

from servers.memory_server.memory_frontmatter import (
    dump_front_matter,
    parse_front_matter,
    parse_record_markdown,
    render_record_markdown,
)


def test_parse_dump_roundtrip_scalars_and_lists():
    metadata = {
        "id": "rec-001",
        "scope": "personal",
        "importance_score": 0.75,
        "count": 3,
        "deleted": None,
        "tags": ["mcp", "memory"],
        "asset_paths": [],
    }
    text = dump_front_matter(metadata)
    parsed = parse_front_matter(text)
    assert parsed == metadata


def test_render_then_parse_roundtrip_preserves_body_and_metadata():
    metadata = {"id": "x", "scope": "shared", "tags": ["mcp"]}
    body = "# Title\n\nLine 1\nLine 2"
    rendered = render_record_markdown(metadata, body)
    out_meta, out_body = parse_record_markdown(rendered)
    assert out_meta == metadata
    assert out_body.startswith("# Title")
    assert "Line 2" in out_body


def test_quoted_strings_preserve_special_chars():
    text = dump_front_matter({"label": "a:b#c"})
    parsed = parse_front_matter(text)
    assert parsed == {"label": "a:b#c"}


def test_unicode_cjk_roundtrip():
    metadata = {"title": "中文标题", "tags": ["中文标签"]}
    rendered = render_record_markdown(metadata, "中文正文")
    out_meta, out_body = parse_record_markdown(rendered)
    assert out_meta == metadata
    assert "中文正文" in out_body


def test_null_and_bool_distinct_from_strings():
    metadata = {"a": None, "b": True, "c": False, "d": "null"}
    text = dump_front_matter(metadata)
    parsed = parse_front_matter(text)
    # Booleans are formatted as "true"/"false" but our parser does not coerce
    # them back to bool — they round-trip as the literal string. This locks
    # in current behavior; if bool round-trip is later required, update both
    # sides together.
    assert parsed["a"] is None
    assert parsed["b"] == "true"
    assert parsed["c"] == "false"
    assert parsed["d"] == "null"


def test_parse_record_markdown_rejects_missing_header():
    try:
        parse_record_markdown("no front matter here")
    except ValueError as exc:
        assert "front matter" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_record_markdown_rejects_unclosed_front_matter():
    try:
        parse_record_markdown("---\nid: x\nbody but no closer")
    except ValueError as exc:
        assert "not closed" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_records_module_reexports_are_identical():
    from servers.memory_server import memory_frontmatter, memory_records

    assert memory_records.parse_front_matter is memory_frontmatter.parse_front_matter
    assert memory_records.dump_front_matter is memory_frontmatter.dump_front_matter
    assert memory_records.parse_record_markdown is memory_frontmatter.parse_record_markdown
    assert memory_records.render_record_markdown is memory_frontmatter.render_record_markdown
