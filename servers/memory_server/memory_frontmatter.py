"""Front Matter parser/dumper for memory records.

Extracted from `memory_records.py` (P1-C). Pure functions; no schema validation.
The small YAML subset supported here is intentional: scalars, quoted strings,
nulls, ints/floats, and one-level lists with `- ` prefixes. This keeps the
truth-source files reviewable in a plain editor without requiring a full YAML
runtime.
"""

from __future__ import annotations

import re
from typing import Any

_SCALAR_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_PACK_ENTRY_START_RE = re.compile(r"^<!-- memory-record-pack-entry id=([^ >]+) -->$")
_PACK_ENTRY_END_RE = re.compile(r"^<!-- /memory-record-pack-entry id=([^ >]+) -->$")
PACK_HEADER = "<!-- memory-record-pack version=1 -->"


def _parse_scalar(raw_value: str) -> Any:
    value = raw_value.strip()
    if value in {"null", "None", "~"}:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    if _SCALAR_RE.match(value):
        if "." in value:
            return float(value)
        return int(value)
    return value


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if (
        not text
        or _SCALAR_RE.match(text)
        or text in {"null", "None", "~", "true", "false"}
        or any(char in text for char in [":", "#", "[", "]", "{", "}", ","])
        or text != text.strip()
    ):
        escaped = text.replace('"', '\\"')
        return f'"{escaped}"'
    return text


def parse_front_matter(front_matter: str) -> dict[str, Any]:
    """Parse the small YAML subset used by memory records."""
    parsed: dict[str, Any] = {}
    current_list_key: str | None = None

    for raw_line in front_matter.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if current_list_key is None:
                raise ValueError("list item found before a key")
            parsed.setdefault(current_list_key, []).append(_parse_scalar(stripped[2:]))
            continue
        if ":" not in stripped:
            raise ValueError(f"invalid front matter line: {raw_line}")
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError("front matter key must not be empty")
        if raw_value.strip() == "":
            parsed[key] = []
            current_list_key = key
        else:
            parsed[key] = _parse_scalar(raw_value)
            current_list_key = None

    return parsed


def parse_record_markdown(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n") and "<!-- memory-record-pack-entry " in text:
        entries = parse_record_pack_entries(text)
        if len(entries) == 1:
            return entries[0]
        raise ValueError("record pack contains multiple entries; use parse_record_pack_entries")
    if not text.startswith("---\n"):
        raise ValueError("record markdown must start with front matter")
    try:
        front_matter, body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError("record markdown front matter is not closed") from exc
    return parse_front_matter(front_matter), body.lstrip("\n")


def render_record_pack_entry(record_id: str, record_markdown: str) -> str:
    """Wrap one rendered record so many records can live in one Markdown file."""
    content = record_markdown.strip()
    return (
        f"<!-- memory-record-pack-entry id={record_id} -->\n"
        f"{content}\n"
        f"<!-- /memory-record-pack-entry id={record_id} -->\n"
    )


def parse_record_pack_entries(text: str) -> list[tuple[dict[str, Any], str]]:
    """Parse either a single record Markdown file or a record pack file.

    Pack files are append-only Markdown containers. Each entry keeps the same
    record Front Matter/body payload used by single-record files, wrapped in
    explicit HTML comments so body-level Markdown horizontal rules do not
    confuse the parser.
    """
    if PACK_HEADER not in text and "<!-- memory-record-pack-entry " not in text:
        return [parse_record_markdown(text)]

    entries: list[tuple[dict[str, Any], str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        start_match = _PACK_ENTRY_START_RE.match(lines[index].strip())
        if not start_match:
            index += 1
            continue
        record_id = start_match.group(1)
        index += 1
        payload_lines: list[str] = []
        closed = False
        while index < len(lines):
            end_match = _PACK_ENTRY_END_RE.match(lines[index].strip())
            if end_match and end_match.group(1) == record_id:
                closed = True
                index += 1
                break
            payload_lines.append(lines[index])
            index += 1
        if not closed:
            raise ValueError(f"record pack entry is not closed: {record_id}")
        metadata, body = parse_record_markdown("\n".join(payload_lines).strip() + "\n")
        entries.append((metadata, body))
    if not entries:
        raise ValueError("record pack contains no entries")
    return entries


def replace_record_pack_entry(text: str, record_id: str, record_markdown: str | None) -> str:
    """Replace or remove one entry in a record pack.

    Passing ``record_markdown=None`` removes the entry. A ValueError is raised
    when the target record id is not present.
    """
    if PACK_HEADER not in text and "<!-- memory-record-pack-entry " not in text:
        raise ValueError("not a record pack")

    lines = text.splitlines()
    out: list[str] = []
    index = 0
    replaced = False
    while index < len(lines):
        start_match = _PACK_ENTRY_START_RE.match(lines[index].strip())
        if not start_match:
            out.append(lines[index])
            index += 1
            continue

        current_id = start_match.group(1)
        entry_lines = [lines[index]]
        index += 1
        closed = False
        while index < len(lines):
            entry_lines.append(lines[index])
            end_match = _PACK_ENTRY_END_RE.match(lines[index].strip())
            index += 1
            if end_match and end_match.group(1) == current_id:
                closed = True
                break
        if not closed:
            raise ValueError(f"record pack entry is not closed: {current_id}")
        if current_id == record_id:
            replaced = True
            if record_markdown is not None:
                out.extend(render_record_pack_entry(record_id, record_markdown).strip().splitlines())
        else:
            out.extend(entry_lines)

    if not replaced:
        raise ValueError(f"record not found in pack: {record_id}")
    return "\n".join(out).rstrip() + "\n"


def dump_front_matter(metadata: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in metadata.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {_format_scalar(item)}")
        else:
            lines.append(f"{key}: {_format_scalar(value)}")
    return "\n".join(lines)


def render_record_markdown(metadata: dict[str, Any], body: str) -> str:
    return f"---\n{dump_front_matter(metadata)}\n---\n\n{body.strip()}\n"


__all__ = [
    "parse_front_matter",
    "parse_record_markdown",
    "parse_record_pack_entries",
    "dump_front_matter",
    "render_record_markdown",
    "render_record_pack_entry",
    "replace_record_pack_entry",
    "PACK_HEADER",
]
