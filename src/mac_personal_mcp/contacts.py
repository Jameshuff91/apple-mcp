"""Apple Contacts database queries.

Uses the AddressBook SQLite DB for contacts, plus a JSON alias file for
handle-to-name mappings (since some iCloud contacts aren't in the local DB).
The alias file lives in the user's config dir, never in the repo.
AppleScript is used for creating/updating contacts (syncs to iCloud).
"""

import json
import sqlite3
import subprocess
from glob import glob
from pathlib import Path

from mac_personal_mcp.config import ALIASES_FILE
from mac_personal_mcp.utils import applescript_string, normalize_phone, strip_apple_label

MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"

# In-memory cache loaded once per process
_aliases_cache: dict[str, str] | None = None


def _load_aliases() -> dict[str, str]:
    """Load handle-to-name aliases from JSON file."""
    global _aliases_cache
    if _aliases_cache is not None:
        return _aliases_cache
    aliases: dict[str, str] = {}
    if ALIASES_FILE.exists():
        with open(ALIASES_FILE) as f:
            aliases = json.load(f)
    _aliases_cache = aliases
    return aliases


def _get_contacts_db_path() -> str:
    """Find the Contacts database under Sources/."""
    base = Path.home() / "Library" / "Application Support" / "AddressBook" / "Sources"
    matches = glob(str(base / "*/AddressBook-v22.abcddb"))
    if not matches:
        raise FileNotFoundError("Contacts database not found under AddressBook/Sources/")
    return matches[0]


def _connect() -> sqlite3.Connection:
    """Open read-only connection to Contacts DB."""
    db_path = _get_contacts_db_path()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_messages() -> sqlite3.Connection:
    """Open read-only connection to Messages DB."""
    conn = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def search_contacts(query: str) -> str:
    """Search contacts by name, email, phone, or organization.

    Args:
        query: Search term to match against contact fields

    Returns:
        Formatted list of matching contacts
    """
    try:
        conn = _connect()
        pattern = f"%{query}%"
        rows = conn.execute(
            """
            SELECT r.Z_PK, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION,
                   r.ZJOBTITLE, r.ZNICKNAME
            FROM ZABCDRECORD r
            WHERE r.Z_ENT = 22
              AND (r.ZFIRSTNAME LIKE ? OR r.ZLASTNAME LIKE ?
                   OR r.ZORGANIZATION LIKE ? OR r.ZNICKNAME LIKE ?
                   OR r.Z_PK IN (
                       SELECT ZOWNER FROM ZABCDEMAILADDRESS WHERE ZADDRESS LIKE ?
                   )
                   OR r.Z_PK IN (
                       SELECT ZOWNER FROM ZABCDPHONENUMBER WHERE ZFULLNUMBER LIKE ?
                   ))
            ORDER BY r.ZFIRSTNAME, r.ZLASTNAME
            LIMIT 20
            """,
            (pattern, pattern, pattern, pattern, pattern, pattern),
        ).fetchall()
        conn.close()

        if not rows:
            return f"No contacts found matching '{query}'."

        results = []
        for r in rows:
            name_parts = [r["ZFIRSTNAME"] or "", r["ZLASTNAME"] or ""]
            name = " ".join(p for p in name_parts if p).strip() or "(No name)"
            line = f"- {name}"
            if r["ZORGANIZATION"]:
                line += f" ({r['ZORGANIZATION']})"
            if r["ZJOBTITLE"]:
                line += f" — {r['ZJOBTITLE']}"
            results.append(line)

        return f"Found {len(rows)} contact(s):\n" + "\n".join(results)
    except Exception as e:
        return f"Error searching contacts: {e}"


def get_contact(name: str) -> str:
    """Get full contact details by name.

    Args:
        name: Contact name to look up

    Returns:
        Full contact card with phone, email, address, etc.
    """
    try:
        conn = _connect()
        pattern = f"%{name}%"
        rows = conn.execute(
            """
            SELECT r.Z_PK, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION,
                   r.ZJOBTITLE, r.ZNICKNAME, r.ZBIRTHDAY
            FROM ZABCDRECORD r
            WHERE r.Z_ENT = 22
              AND (r.ZFIRSTNAME LIKE ? OR r.ZLASTNAME LIKE ?
                   OR (r.ZFIRSTNAME || ' ' || r.ZLASTNAME) LIKE ?)
            LIMIT 5
            """,
            (pattern, pattern, pattern),
        ).fetchall()

        if not rows:
            conn.close()
            return f"No contact found matching '{name}'."

        results = []
        for r in rows:
            pk = r["Z_PK"]
            name_parts = [r["ZFIRSTNAME"] or "", r["ZLASTNAME"] or ""]
            full_name = " ".join(p for p in name_parts if p).strip() or "(No name)"

            lines = [f"## {full_name}"]
            if r["ZNICKNAME"]:
                lines.append(f"Nickname: {r['ZNICKNAME']}")
            if r["ZORGANIZATION"]:
                lines.append(f"Organization: {r['ZORGANIZATION']}")
            if r["ZJOBTITLE"]:
                lines.append(f"Title: {r['ZJOBTITLE']}")

            phones = conn.execute(
                "SELECT ZLABEL, ZFULLNUMBER FROM ZABCDPHONENUMBER WHERE ZOWNER = ?",
                (pk,),
            ).fetchall()
            for p in phones:
                label = strip_apple_label(p["ZLABEL"])
                lines.append(f"Phone ({label}): {p['ZFULLNUMBER']}")

            emails = conn.execute(
                "SELECT ZLABEL, ZADDRESS FROM ZABCDEMAILADDRESS WHERE ZOWNER = ?",
                (pk,),
            ).fetchall()
            for e in emails:
                label = strip_apple_label(e["ZLABEL"])
                lines.append(f"Email ({label}): {e['ZADDRESS']}")

            addresses = conn.execute(
                """SELECT ZLABEL, ZSTREET, ZCITY, ZSTATE, ZZIPCODE, ZCOUNTRYNAME
                   FROM ZABCDPOSTALADDRESS WHERE ZOWNER = ?""",
                (pk,),
            ).fetchall()
            for a in addresses:
                label = strip_apple_label(a["ZLABEL"])
                addr_parts = [
                    a["ZSTREET"] or "",
                    a["ZCITY"] or "",
                    a["ZSTATE"] or "",
                    a["ZZIPCODE"] or "",
                    a["ZCOUNTRYNAME"] or "",
                ]
                addr = ", ".join(p for p in addr_parts if p)
                if addr:
                    lines.append(f"Address ({label}): {addr}")

            results.append("\n".join(lines))

        conn.close()
        return "\n\n".join(results)
    except Exception as e:
        return f"Error getting contact: {e}"


def resolve_handle(handle: str) -> dict[str, str] | None:
    """Resolve a Messages handle (phone/email) to a contact name.

    Checks in order: the alias file, AddressBook DB, iMessage chat display names.

    Args:
        handle: Phone number or email address from Messages

    Returns:
        Dict with 'name' key, or None if no match
    """
    # 1. Check aliases file first (fastest, user-curated)
    aliases = _load_aliases()
    handle_normalized = normalize_phone(handle) if "@" not in handle else handle.lower()
    for alias_handle, alias_name in aliases.items():
        if "@" in handle:
            if alias_handle.lower() == handle_normalized:
                return {"name": alias_name}
        else:
            if normalize_phone(alias_handle) == handle_normalized:
                return {"name": alias_name}

    # 2. Check AddressBook DB
    try:
        conn = _connect()
        if "@" in handle:
            row = conn.execute(
                """
                SELECT r.ZFIRSTNAME, r.ZLASTNAME
                FROM ZABCDRECORD r
                JOIN ZABCDEMAILADDRESS e ON e.ZOWNER = r.Z_PK
                WHERE r.Z_ENT = 22 AND LOWER(e.ZADDRESS) = LOWER(?)
                LIMIT 1
                """,
                (handle,),
            ).fetchone()
        else:
            rows = conn.execute(
                """
                SELECT r.ZFIRSTNAME, r.ZLASTNAME, p.ZFULLNUMBER
                FROM ZABCDRECORD r
                JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK
                WHERE r.Z_ENT = 22
                """,
            ).fetchall()
            row = None
            for r in rows:
                if r["ZFULLNUMBER"] and normalize_phone(r["ZFULLNUMBER"]) == handle_normalized:
                    row = r
                    break
        conn.close()

        if row:
            name_parts = [row["ZFIRSTNAME"] or "", row["ZLASTNAME"] or ""]
            name = " ".join(p for p in name_parts if p).strip()
            if name:
                return {"name": name}
    except Exception:
        pass

    # 3. Check iMessage chat display names as fallback
    try:
        msg_conn = _connect_messages()
        chat_row = msg_conn.execute(
            """
            SELECT display_name FROM chat
            WHERE chat_identifier LIKE ?
              AND display_name IS NOT NULL AND display_name != ''
            LIMIT 1
            """,
            (f"%{handle}%",),
        ).fetchone()
        msg_conn.close()
        if chat_row and chat_row["display_name"]:
            return {"name": chat_row["display_name"]}
    except Exception:
        pass

    return None


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


def add_contact(
    first_name: str,
    last_name: str = "",
    phone: str = "",
    email: str = "",
    organization: str = "",
) -> str:
    """Create a new contact in Apple Contacts (syncs to iCloud).

    Also saves the handle to the local alias file for instant resolution.

    Args:
        first_name: First name
        last_name: Last name (optional)
        phone: Phone number (optional)
        email: Email address (optional)
        organization: Company/organization (optional)

    Returns:
        Confirmation message
    """
    try:
        safe_first = applescript_string(first_name)
        safe_last = applescript_string(last_name)

        props = f'first name:"{safe_first}"'
        if last_name:
            props += f', last name:"{safe_last}"'
        if organization:
            safe_org = applescript_string(organization)
            props += f', organization:"{safe_org}"'

        script_lines = [
            'tell application "Contacts"',
            f"    set newPerson to make new person with properties {{{props}}}",
        ]

        if phone:
            safe_phone = applescript_string(phone)
            script_lines.append(
                f'    make new phone at end of phones of newPerson '
                f'with properties {{label:"mobile", value:"{safe_phone}"}}'
            )

        if email:
            safe_email = applescript_string(email)
            script_lines.append(
                f'    make new email at end of emails of newPerson '
                f'with properties {{label:"home", value:"{safe_email}"}}'
            )

        script_lines.append("    save")
        script_lines.append("end tell")

        script = "\n".join(script_lines)
        _run_applescript(script)

        # Also save to alias file for instant resolution
        full_name = f"{first_name} {last_name}".strip()
        if phone:
            _save_alias(phone, full_name)
        if email:
            _save_alias(email, full_name)

        return f"Contact '{full_name}' created successfully" + (
            f" with phone {phone}" if phone else ""
        ) + (f" and email {email}" if email else "") + "."
    except Exception as e:
        return f"Error creating contact: {e}"


def _save_alias(handle: str, name: str) -> None:
    """Save a handle-to-name mapping in the alias file."""
    global _aliases_cache
    aliases = _load_aliases()
    aliases[handle] = name
    _aliases_cache = aliases
    ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ALIASES_FILE, "w") as f:
        json.dump(aliases, f, indent=4)
        f.write("\n")


def save_alias(handle: str, name: str) -> str:
    """Save a handle-to-name alias without creating an Apple Contact.

    Args:
        handle: Phone number or email address
        name: Display name for this handle

    Returns:
        Confirmation message
    """
    try:
        _save_alias(handle, name)
        return f"Alias saved: {handle} → {name}"
    except Exception as e:
        return f"Error saving alias: {e}"


def list_unresolved_handles(min_messages: int = 5) -> str:
    """List iMessage handles that don't resolve to any contact name.

    Args:
        min_messages: Minimum message count to include (default 5)

    Returns:
        List of unresolved handles with message counts and sample messages
    """
    try:
        msg_conn = _connect_messages()
        rows = msg_conn.execute(
            """
            SELECT c.chat_identifier, COUNT(m.ROWID) as msg_count
            FROM chat c
            JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
            JOIN message m ON cmj.message_id = m.ROWID
            WHERE c.chat_identifier LIKE '+%'
            GROUP BY c.ROWID
            HAVING msg_count >= ?
            ORDER BY msg_count DESC
            """,
            (min_messages,),
        ).fetchall()
        msg_conn.close()

        unresolved = []
        for r in rows:
            handle = r["chat_identifier"]
            if resolve_handle(handle) is None:
                unresolved.append(f"- {handle} ({r['msg_count']} messages)")

        if not unresolved:
            return f"All handles with {min_messages}+ messages are resolved."

        return f"Unresolved handles ({len(unresolved)}):\n" + "\n".join(unresolved)
    except Exception as e:
        return f"Error listing unresolved handles: {e}"
