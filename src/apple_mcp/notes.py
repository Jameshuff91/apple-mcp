"""Apple Notes reads (AppleScript + protobuf) and writes (AppleScript + GUI scripting)."""

import gzip
import html
import re
import sqlite3
import subprocess
from pathlib import Path

from apple_mcp.utils import applescript_string

NOTES_DB = (
    Path.home()
    / "Library"
    / "Group Containers"
    / "group.com.apple.notes"
    / "NoteStore.sqlite"
)


def _run_applescript(script: str) -> str:
    """Run an AppleScript and return its output."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()


def _strip_html(html: str) -> str:
    """Strip HTML tags and decode entities for plain text."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&nbsp;", " ")
    return text.strip()


# ── Read operations (AppleScript) ──


def list_folders() -> str:
    """List all Apple Notes folders.

    Returns:
        Formatted list of folder names
    """
    try:
        script = """
            tell application "Notes"
                set folderNames to {}
                repeat with f in folders
                    set end of folderNames to name of f
                end repeat
                set AppleScript's text item delimiters to linefeed
                return folderNames as text
            end tell
        """
        output = _run_applescript(script)
        if not output:
            return "No folders found."
        folders = output.split("\n")
        return "Notes folders:\n" + "\n".join(f"- {f}" for f in folders)
    except Exception as e:
        return f"Error listing folders: {e}"


def list_notes(folder: str = "", search: str = "", limit: int = 20) -> str:
    """List Apple Notes, optionally filtered by folder or search term.

    Args:
        folder: Optional folder name to filter by
        search: Optional search term to filter note titles
        limit: Maximum notes to return (default 20)

    Returns:
        Formatted list of notes with titles and modification dates
    """
    try:
        if folder:
            safe_folder = applescript_string(folder)
            script = f"""
                tell application "Notes"
                    set noteList to {{}}
                    set theFolder to folder "{safe_folder}"
                    repeat with n in notes of theFolder
                        set noteInfo to (name of n) & "|||" & (modification date of n as string)
                        set end of noteList to noteInfo
                    end repeat
                    set AppleScript's text item delimiters to linefeed
                    return noteList as text
                end tell
            """
        else:
            script = """
                tell application "Notes"
                    set noteList to {}
                    repeat with n in notes
                        set noteInfo to (name of n) & "|||" & (modification date of n as string)
                        set end of noteList to noteInfo
                    end repeat
                    set AppleScript's text item delimiters to linefeed
                    return noteList as text
                end tell
            """
        output = _run_applescript(script)
        if not output:
            return "No notes found."

        lines = output.split("\n")
        results = []
        search_lower = search.lower() if search else ""

        for line in lines:
            if len(results) >= limit:
                break
            parts = line.split("|||")
            title = parts[0].strip() if parts else line.strip()
            mod_date = parts[1].strip() if len(parts) > 1 else ""

            if search_lower and search_lower not in title.lower():
                continue

            entry = f"- {title}"
            if mod_date:
                entry += f" (modified: {mod_date})"
            results.append(entry)

        if not results:
            return f"No notes found" + (f" matching '{search}'" if search else "") + "."

        return f"Notes ({len(results)}):\n" + "\n".join(results)
    except Exception as e:
        return f"Error listing notes: {e}"


def read_note(title: str) -> str:
    """Read the content of an Apple Note by title.

    Args:
        title: The title of the note to read

    Returns:
        Plain text content of the note
    """
    try:
        safe_title = applescript_string(title)
        script = f"""
            tell application "Notes"
                set matchingNotes to notes whose name is "{safe_title}"
                if (count of matchingNotes) is 0 then
                    return "NOTE_NOT_FOUND"
                end if
                set theNote to item 1 of matchingNotes
                return body of theNote
            end tell
        """
        output = _run_applescript(script)
        if output == "NOTE_NOT_FOUND":
            return f"Note '{title}' not found."
        return f"## {title}\n\n{_strip_html(output)}"
    except Exception as e:
        return f"Error reading note: {e}"


# ── Checklist reading (SQLite + protobuf) ──


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a protobuf varint at position, returning (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if (b & 0x80) == 0:
            break
        shift += 7
    return result, pos


def _parse_protobuf_field(data: bytes, pos: int) -> tuple[int, int, bytes | int, int]:
    """Parse one protobuf field, returning (field_number, wire_type, value, new_pos)."""
    if pos >= len(data):
        raise StopIteration
    tag, pos = _decode_varint(data, pos)
    field_number = tag >> 3
    wire_type = tag & 0x07

    if wire_type == 0:  # varint
        value, pos = _decode_varint(data, pos)
        return field_number, wire_type, value, pos
    elif wire_type == 2:  # length-delimited
        length, pos = _decode_varint(data, pos)
        value = data[pos : pos + length]
        return field_number, wire_type, value, pos + length
    elif wire_type == 5:  # 32-bit
        value = data[pos : pos + 4]
        return field_number, wire_type, value, pos + 4
    elif wire_type == 1:  # 64-bit
        value = data[pos : pos + 8]
        return field_number, wire_type, value, pos + 8
    else:
        raise ValueError(f"Unknown wire type {wire_type} at pos {pos}")


def _parse_all_fields(data: bytes) -> list[tuple[int, int, bytes | int]]:
    """Parse all protobuf fields from data."""
    fields: list[tuple[int, int, bytes | int]] = []
    pos = 0
    while pos < len(data):
        try:
            field_number, wire_type, value, pos = _parse_protobuf_field(data, pos)
            fields.append((field_number, wire_type, value))
        except (StopIteration, ValueError):
            break
    return fields


def _get_bytes(val: bytes | int) -> bytes:
    """Type-narrow a protobuf field value known to be bytes (wire_type 2)."""
    assert isinstance(val, bytes)
    return val


def read_checklist(title: str) -> str:
    """Read a checklist from Apple Notes using protobuf parsing.

    Args:
        title: The title of the note containing the checklist

    Returns:
        Formatted checklist with checked/unchecked status
    """
    try:
        conn = sqlite3.connect(f"file:{NOTES_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        row = conn.execute(
            """
            SELECT nd.ZDATA, n.ZTITLE1 as title
            FROM ZICCLOUDSYNCINGOBJECT n
            JOIN ZICNOTEDATA nd ON nd.ZNOTE = n.Z_PK
            WHERE n.ZTITLE1 LIKE ?
              AND nd.ZDATA IS NOT NULL
            LIMIT 1
            """,
            (f"%{title}%",),
        ).fetchone()
        conn.close()

        if not row:
            return f"No checklist note found matching '{title}'."

        raw_data = row["ZDATA"]
        if raw_data[:2] == b"\x1f\x8b":
            raw_data = gzip.decompress(raw_data)

        # Parse the top-level protobuf
        top_fields = _parse_all_fields(raw_data)

        # Navigate to f2 (document)
        f2_data = None
        for fn, wt, val in top_fields:
            if fn == 2 and wt == 2:
                f2_data = val
                break

        if not f2_data:
            return f"Could not parse note data for '{title}'."

        doc_fields = _parse_all_fields(_get_bytes(f2_data))

        # Navigate to f2.f3 which contains text (f3.f2), CRDT ops (f3.f3), and styles (f3.f5)
        f3_data = None
        for fn, wt, val in doc_fields:
            if fn == 3 and wt == 2:
                f3_data = _get_bytes(val)
                break

        if not f3_data:
            return f"Could not parse note data for '{title}'."

        f3_fields = _parse_all_fields(f3_data)

        # Get text content from f3.f2
        text_content = ""
        for fn3, wt3, val3 in f3_fields:
            if fn3 == 2 and wt3 == 2:
                text_content = _get_bytes(val3).decode("utf-8", errors="ignore")
                break

        if not text_content:
            return f"No text content found in '{title}'."

        # Get paragraph styles from f3.f5 entries
        # Each f5 entry: f5.f1 = text length, f5.f2.f1 = style type (103=checklist),
        # f5.f2.f5.f2 = checked state (0=unchecked, 1=checked)
        paragraph_styles: list[dict[str, int | bool]] = []
        text_lengths: list[int] = []

        for fn3, wt3, val3 in f3_fields:
            if fn3 == 5 and wt3 == 2:
                f5_fields = _parse_all_fields(_get_bytes(val3))
                style_info: dict[str, int | bool] = {"is_checklist": False, "checked": False}

                for fn5, wt5, val5 in f5_fields:
                    if fn5 == 1 and wt5 == 0:
                        text_lengths.append(int(val5))
                    if fn5 == 2 and wt5 == 2:
                        style_fields = _parse_all_fields(_get_bytes(val5))
                        for fns, wts, vals in style_fields:
                            if fns == 1 and wts == 0 and int(vals) == 103:
                                style_info["is_checklist"] = True
                            if fns == 5 and wts == 2:
                                checklist_fields = _parse_all_fields(_get_bytes(vals))
                                for fnc, wtc, valc in checklist_fields:
                                    if fnc == 2 and wtc == 0:
                                        style_info["checked"] = int(valc) == 1

                paragraph_styles.append(style_info)

        # Map text to paragraphs using lengths
        lines = []
        pos = 0
        for i, length in enumerate(text_lengths):
            line_text = text_content[pos : pos + length].rstrip("\n")
            pos += length

            if i < len(paragraph_styles) and paragraph_styles[i]["is_checklist"]:
                check = "[x]" if paragraph_styles[i]["checked"] else "[ ]"
                lines.append(f"- {check} {line_text}")
            elif line_text:
                lines.append(line_text)

        if not lines:
            return f"No checklist items found in '{title}'."

        return f"## {row['title']}\n\n" + "\n".join(lines)
    except Exception as e:
        return f"Error reading checklist: {e}"


# ── Write operations (AppleScript + Shortcuts) ──


def create_note(title: str, body: str, folder: str = "") -> str:
    """Create a new Apple Note.

    Args:
        title: Title for the new note
        body: Body text (plain text, will be wrapped in HTML)
        folder: Optional folder name to create the note in

    Returns:
        Confirmation message
    """
    try:
        safe_title = applescript_string(title)
        # Notes stores HTML; escape user text before converting newlines to <br>
        html_body = f"<h1>{html.escape(title)}</h1>" + html.escape(body).replace("\n", "<br>")
        safe_body = applescript_string(html_body)

        if folder:
            safe_folder = applescript_string(folder)
            script = f"""
                tell application "Notes"
                    set theFolder to folder "{safe_folder}"
                    make new note at theFolder with properties {{name:"{safe_title}", body:"{safe_body}"}}
                    return "Created"
                end tell
            """
        else:
            script = f"""
                tell application "Notes"
                    make new note with properties {{name:"{safe_title}", body:"{safe_body}"}}
                    return "Created"
                end tell
            """
        _run_applescript(script)
        return f"Note '{title}' created successfully."
    except Exception as e:
        return f"Error creating note: {e}"


def update_note(title: str, body: str) -> str:
    """Update an existing Apple Note's body.

    Args:
        title: Title of the note to update
        body: New body text (plain text)

    Returns:
        Confirmation message
    """
    try:
        safe_title = applescript_string(title)
        html_body = html.escape(body).replace("\n", "<br>")
        safe_body = applescript_string(html_body)

        script = f"""
            tell application "Notes"
                set matchingNotes to notes whose name is "{safe_title}"
                if (count of matchingNotes) is 0 then
                    return "NOTE_NOT_FOUND"
                end if
                set theNote to item 1 of matchingNotes
                set body of theNote to "{safe_body}"
                return "Updated"
            end tell
        """
        output = _run_applescript(script)
        if output == "NOTE_NOT_FOUND":
            return f"Note '{title}' not found."
        return f"Note '{title}' updated successfully."
    except Exception as e:
        return f"Error updating note: {e}"


def _gui_open_note(title: str) -> None:
    """Open a note in Notes.app via GUI scripting."""
    safe_title = applescript_string(title)
    _run_applescript(f"""
        tell application "Notes"
            activate
            set matchingNotes to notes whose name is "{safe_title}"
            if (count of matchingNotes) is 0 then
                error "Note not found"
            end if
            show item 1 of matchingNotes
        end tell
    """)
    # Wait for note to fully render
    _run_applescript('delay 1')


def _gui_keystroke(text: str) -> None:
    """Type text via System Events."""
    safe_text = applescript_string(text)
    _run_applescript(f"""
        tell application "System Events"
            tell process "Notes"
                keystroke "{safe_text}"
            end tell
        end tell
    """)


def _gui_key_code(code: int, modifiers: str = "") -> None:
    """Press a key code with optional modifiers via System Events."""
    mod_str = f" using {{{modifiers}}}" if modifiers else ""
    _run_applescript(f"""
        tell application "System Events"
            tell process "Notes"
                key code {code}{mod_str}
            end tell
        end tell
    """)


def _gui_click_menu(menu: str, item: str) -> None:
    """Click a menu item via System Events."""
    _run_applescript(f"""
        tell application "System Events"
            tell process "Notes"
                click menu item "{item}" of menu "{menu}" of menu bar 1
            end tell
        end tell
    """)


def add_checklist_item(note_title: str, item_text: str) -> str:
    """Add a checklist item to an existing note via GUI scripting.

    Opens the note in Notes.app, navigates to the end, enables checklist
    mode, and types the item text.

    Args:
        note_title: Title of the note to add the item to
        item_text: Text of the checklist item to add

    Returns:
        Confirmation message
    """
    try:
        _gui_open_note(note_title)

        # Go to end of note: Cmd+End
        _gui_key_code(119, "command down")
        _run_applescript("delay 0.3")

        # New line
        _gui_key_code(36)  # Return
        _run_applescript("delay 0.3")

        # Enable checklist mode via Format > Checklist
        _gui_click_menu("Format", "Checklist")
        _run_applescript("delay 0.3")

        # Type the item text
        _gui_keystroke(item_text)
        _run_applescript("delay 0.3")

        return f"Added '{item_text}' to checklist in '{note_title}'."
    except Exception as e:
        return f"Error adding checklist item: {e}"


def set_checklist_done(note_title: str, item_text: str, done: bool = True) -> str:
    """Toggle a checklist item's checked state via GUI scripting.

    Opens the note, finds the item text, and uses Format > Mark as Checked/Unchecked.

    Args:
        note_title: Title of the note containing the checklist
        item_text: Text of the checklist item to toggle
        done: True to mark as done, False to mark as undone

    Returns:
        Confirmation message
    """
    try:
        _gui_open_note(note_title)

        # Use Find to locate the item: Cmd+F
        _gui_key_code(3, "command down")  # Cmd+F
        _run_applescript("delay 0.5")

        # Type the item text to search
        _gui_keystroke(item_text)
        _run_applescript("delay 0.5")

        # Press Enter to find it
        _gui_key_code(36)  # Return
        _run_applescript("delay 0.3")

        # Close find bar: Escape
        _gui_key_code(53)  # Escape
        _run_applescript("delay 0.3")

        # Toggle checked state
        menu_item = "Mark as Checked" if done else "Mark as Unchecked"
        _gui_click_menu("Format", menu_item)
        _run_applescript("delay 0.3")

        status = "done" if done else "not done"
        return f"Marked '{item_text}' as {status} in '{note_title}'."
    except Exception as e:
        return f"Error toggling checklist item: {e}"


def create_note_with_checklist(title: str, items: list[str]) -> str:
    """Create a new note with a checklist via GUI scripting.

    Creates the note via AppleScript, then uses GUI scripting to add
    checklist items with proper formatting.

    Args:
        title: Title for the new note
        items: List of checklist item texts (all start unchecked)

    Returns:
        Confirmation message
    """
    try:
        # Create the note via AppleScript
        safe_title = applescript_string(title)
        safe_heading = applescript_string(html.escape(title))
        _run_applescript(f"""
            tell application "Notes"
                make new note with properties {{name:"{safe_title}", body:"<div><b>{safe_heading}</b></div>"}}
            end tell
        """)

        # Open it and add checklist items
        _gui_open_note(title)

        # Go to end
        _gui_key_code(119, "command down")
        _run_applescript("delay 0.3")

        for i, item in enumerate(items):
            # New line
            _gui_key_code(36)  # Return
            _run_applescript("delay 0.2")

            # Enable checklist mode (toggles, so only on first item)
            if i == 0:
                _gui_click_menu("Format", "Checklist")
                _run_applescript("delay 0.2")

            # Type the item
            _gui_keystroke(item)
            _run_applescript("delay 0.2")

        return f"Created checklist note '{title}' with {len(items)} item(s)."
    except Exception as e:
        return f"Error creating checklist note: {e}"
