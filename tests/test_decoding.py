"""Tests for the undocumented-format decoders, using synthetic data only."""

import gzip
import sqlite3
from pathlib import Path

import pytest

from apple_mcp import notes
from apple_mcp.messages import extract_message_text
from apple_mcp.utils import normalize_phone


def test_normalize_phone() -> None:
    assert normalize_phone("+1 (202) 555-0123") == "2025550123"
    assert normalize_phone("202.555.0123") == "2025550123"


def _typedstream_blob(text: str) -> bytes:
    body = text.encode()
    if len(body) < 0x80:
        length = bytes([len(body)])
    else:
        length = b"\x81" + len(body).to_bytes(2, "little")
    return b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + length + body + b"\x86\x84"


def test_attributed_body_short_text() -> None:
    assert extract_message_text(None, _typedstream_blob("See you Saturday")) == "See you Saturday"


def test_attributed_body_long_text_uses_wide_length() -> None:
    long_text = "x" * 300
    assert extract_message_text(None, _typedstream_blob(long_text)) == long_text


def test_text_column_wins() -> None:
    assert extract_message_text("plain", _typedstream_blob("ignored")) == "plain"


# ── Minimal protobuf writer for building a synthetic Notes document ──


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field_varint(num: int, value: int) -> bytes:
    return _varint(num << 3) + _varint(value)


def _field_bytes(num: int, value: bytes) -> bytes:
    return _varint((num << 3) | 2) + _varint(len(value)) + value


def _paragraph(length: int, checklist: bool, checked: bool = False) -> bytes:
    style = _field_varint(1, 103 if checklist else 0)
    if checklist:
        style += _field_bytes(5, _field_varint(2, 1 if checked else 0))
    return _field_bytes(5, _field_varint(1, length) + _field_bytes(2, style))


def _note_blob(lines: list[tuple[str, bool, bool]]) -> bytes:
    text = "".join(line + "\n" for line, _, _ in lines)
    note = _field_bytes(2, text.encode())
    for line, is_checklist, checked in lines:
        note += _paragraph(len(line) + 1, is_checklist, checked)
    return gzip.compress(_field_bytes(2, _field_bytes(3, note)))


def test_read_checklist_from_protobuf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "NoteStore.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER PRIMARY KEY, ZTITLE1 TEXT)")
    conn.execute("CREATE TABLE ZICNOTEDATA (ZNOTE INTEGER, ZDATA BLOB)")
    conn.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (1, 'Groceries')")
    blob = _note_blob([("Groceries", False, False), ("Milk", True, True), ("Eggs", True, False)])
    conn.execute("INSERT INTO ZICNOTEDATA VALUES (1, ?)", (blob,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(notes, "NOTES_DB", db)

    result = notes.read_checklist("Groceries")
    assert result == "## Groceries\n\nGroceries\n- [x] Milk\n- [ ] Eggs"
