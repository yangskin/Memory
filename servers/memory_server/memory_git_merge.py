"""按完整记录三方合并；损坏或同 ID 不同内容必须报冲突。"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .memory_frontmatter import PACK_HEADER, canonical_record, parse_record_markdown, sparse_record_metadata

_ENTRY = re.compile(r"^<!-- memory-record-pack-entry id=([^ >]+) -->\n(.*?)^<!-- /memory-record-pack-entry id=\1 -->[ \t]*(?:\n|$)", re.M | re.S)
_AUDIT = re.compile(r"^<!-- last overwritten by [^\s<>]+ at (\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC) -->$")


@dataclass(frozen=True)
class MergeRecord:
    metadata: dict[str, Any]
    body: str
    payload: str


class RecordPack(dict[str, MergeRecord]):
    """保留旧 Markdown writer 的审计注释，重渲染或合并时不得丢弃。"""

    def __init__(self, *args, audit_comments=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.audit_comments = tuple(audit_comments)


def _audit_comments(gap: str) -> list[str]:
    comments = []
    for line in gap.splitlines():
        if not line.strip():
            continue
        match = _AUDIT.fullmatch(line)
        if not match:
            raise ValueError('incomplete record pack or unframed content')
        # 仅接受旧 writer 的精确语法和有效日期，任意 HTML 注释仍是损坏。
        datetime.strptime(match[1], '%Y-%m-%d %H:%M UTC')
        comments.append(line)
    return comments


def read_pack(text: str, *, allow_empty=False) -> dict[str, MergeRecord]:
    text = text.replace('\r\n', '\n')
    if not text.strip() and allow_empty:
        return {}
    if not text.startswith(PACK_HEADER + '\n'):
        raise ValueError('record pack header is missing or unsupported')
    remainder = text[len(PACK_HEADER):]
    records = RecordPack()
    audits = []
    cursor = 0
    for match in _ENTRY.finditer(remainder):
        audits.extend(_audit_comments(remainder[cursor:match.start()]))
        record_id, payload = match.groups()
        metadata, body = parse_record_markdown(payload)
        if metadata.get('id') != record_id or not metadata.get('record_kind'):
            raise ValueError('record boundary ID does not match metadata')
        if str(metadata.get('schema_version')) not in {'1.0', '2.0'}:
            raise ValueError('unsupported record schema; preserve the original file')
        item = MergeRecord(metadata, body, payload.rstrip() + '\n')
        if record_id in records and canonical_record(metadata, body) != canonical_record(records[record_id].metadata, records[record_id].body):
            raise ValueError(f'conflicting duplicate record ID: {record_id}')
        records[record_id] = item
        cursor = match.end()
    audits.extend(_audit_comments(remainder[cursor:]))
    if not records:
        raise ValueError('incomplete record pack or unframed content')
    records.audit_comments = tuple(audits)
    return records


def compact_payload(item: MergeRecord) -> str:
    """只删已知空字段的原始行，不重新转义旧 YAML 或改正文。"""
    front, body = item.payload[4:].split('\n---\n', 1)
    keep = sparse_record_metadata(item.metadata)
    lines = []
    keeping = True
    for line in front.splitlines():
        if line and not line[0].isspace() and ':' in line:
            keeping = line.split(':', 1)[0].strip() in keep
        if keeping:
            lines.append(line)
    return '---\n' + '\n'.join(lines) + '\n---\n' + body


def render_pack(records: dict[str, MergeRecord], *, compact=False) -> str:
    parts = [PACK_HEADER + '\n']
    for record_id in sorted(records):
        item = records[record_id]
        payload = compact_payload(item) if compact else item.payload
        parts.append(f'<!-- memory-record-pack-entry id={record_id} -->\n{payload.rstrip()}\n<!-- /memory-record-pack-entry id={record_id} -->\n')
    parts.extend(comment + '\n' for comment in getattr(records, 'audit_comments', ()))
    return '\n'.join(parts)


def merge_packs(base: str, left: str, right: str) -> str:
    ancestor = read_pack(base, allow_empty=True)
    ours, theirs = read_pack(left), read_pack(right)
    if not set(ancestor).issubset(ours) or not set(ancestor).issubset(theirs):
        raise ValueError('record removal requires explicit resolution; refusing to resurrect or discard records')
    audits = Counter()
    for source in (ancestor, ours, theirs):
        audits |= Counter(getattr(source, 'audit_comments', ()))
    merged = RecordPack(audit_comments=sorted(audits.elements()))
    for record_id in set(ancestor) | set(ours) | set(theirs):
        variants = [version[record_id] for version in (ancestor, ours, theirs) if record_id in version]
        canonical = canonical_record(variants[0].metadata, variants[0].body)
        if any(canonical_record(item.metadata, item.body) != canonical for item in variants[1:]):
            raise ValueError(f'record content conflict: {record_id}')
        merged[record_id] = min(variants, key=lambda item: (len(item.payload), item.payload))
    return render_pack(merged)
