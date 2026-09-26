"""iMessage database queries, NSKeyedArchiver text extraction, and sending."""

import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mac_personal_mcp.config import ALLOW_NEW_RECIPIENTS
from mac_personal_mcp.contacts import get_contact, resolve_handle
from mac_personal_mcp.utils import (
    applescript_string,
    apple_messages_date_to_datetime,
    datetime_to_apple_messages_ns,
    format_datetime,
    normalize_phone,
)

MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"

TAPBACK_MAP = {
    2000: "Loved",
    2001: "Liked",
    2002: "Disliked",
    2003: "Laughed",
    2004: "Emphasized",
    2005: "Questioned",
    2006: "Custom emoji",
}

# Class names to filter out when extracting text from attributedBody
_OBJC_CLASS_NAMES = {
    "NSString",
    "NSDictionary",
    "NSAttributedString",
    "NSMutableAttributedString",
    "NSMutableDictionary",
    "NSMutableString",
    "NSArray",
    "NSMutableArray",
    "NSNumber",
    "NSValue",
    "NSObject",
    "NSParagraphStyle",
    "NSMutableParagraphStyle",
    "NSFont",
    "NSColor",
}


def _connect() -> sqlite3.Connection:
    """Open read-only connection to Messages DB."""
    conn = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def extract_message_text(text_col: str | None, attributed_body: bytes | None) -> str | None:
    """Extract text, preferring text column, falling back to attributedBody blob.

    The attributedBody column stores NSKeyedArchiver/typedstream data.
    ~96% of messages use this instead of the plain text column.
    """
    if text_col:
        return text_col
    if not attributed_body:
        return None

    # typedstream format: find NSString marker, then '+' byte, then length+text
    nsstring_idx = attributed_body.find(b"NSString")
    if nsstring_idx < 0:
        return _extract_fallback(attributed_body)

    for i in range(nsstring_idx, min(nsstring_idx + 100, len(attributed_body))):
        if attributed_body[i : i + 1] == b"+":
            decoded = _typedstream_length(attributed_body, i + 1)
            if decoded is not None:
                length, text_offset = decoded
                if 0 < length <= len(attributed_body) - text_offset:
                    text = attributed_body[text_offset : text_offset + length].decode(
                        "utf-8", errors="ignore"
                    )
                    return text.rstrip()

    return _extract_fallback(attributed_body)


def _typedstream_length(data: bytes, offset: int) -> tuple[int, int] | None:
    """Decode the compact integer preceding an NSString in typedstream data."""
    if offset >= len(data):
        return None
    marker = data[offset]
    widths = {0x81: 2, 0x82: 4, 0x83: 8}
    if marker not in widths:
        return (marker, offset + 1) if marker < 0x80 else None
    width = widths[marker]
    start = offset + 1
    end = start + width
    if end > len(data):
        return None
    length = int.from_bytes(data[start:end], byteorder="little", signed=True)
    return (length, end) if length > 0 else None


def _extract_fallback(data: bytes) -> str | None:
    """Fallback: extract readable UTF-8 sequences, filtering ObjC class names."""
    # Find sequences of printable ASCII/UTF-8
    segments = []
    current: list[int] = []
    for b in data:
        if 32 <= b < 127 or b >= 0xC0:  # printable ASCII or UTF-8 start byte
            current.append(b)
        else:
            if len(current) >= 4:
                try:
                    text = bytes(current).decode("utf-8", errors="ignore").strip()
                    if text and text not in _OBJC_CLASS_NAMES and not text.startswith("com.apple."):
                        segments.append(text)
                except Exception:
                    pass
            current = []

    if current and len(current) >= 4:
        try:
            text = bytes(current).decode("utf-8", errors="ignore").strip()
            if text and text not in _OBJC_CLASS_NAMES and not text.startswith("com.apple."):
                segments.append(text)
        except Exception:
            pass

    return " ".join(segments) if segments else None


def _enrich_handle(handle_id: str | None) -> str:
    """Resolve handle to contact name if possible."""
    if not handle_id:
        return "Unknown"
    contact = resolve_handle(handle_id)
    if contact:
        return f"{contact['name']} ({handle_id})"
    return handle_id


def _format_message(row: sqlite3.Row) -> str:
    """Format a message row for display."""
    text = extract_message_text(row["text"], row["attributedBody"])
    dt = apple_messages_date_to_datetime(row["date"])
    date_str = format_datetime(dt)

    assoc_type = row["associated_message_type"] if "associated_message_type" in row.keys() else 0

    if assoc_type and assoc_type in TAPBACK_MAP:
        sender = "You" if row["is_from_me"] else _enrich_handle(row["handle_id"])
        return f"[{date_str}] {sender} reacted {TAPBACK_MAP[assoc_type]}"

    sender = "You" if row["is_from_me"] else _enrich_handle(row["handle_id"])
    return f"[{date_str}] {sender}: {text or '(attachment/no text)'}"


def search_messages(query: str, contact: str = "", days_back: int = 30, limit: int = 20) -> str:
    """Search iMessage conversations by text content.

    Args:
        query: Text to search for in messages
        contact: Optional contact name or handle to filter by
        days_back: How many days back to search (default 30)
        limit: Maximum results to return (default 20)

    Returns:
        Formatted list of matching messages
    """
    try:
        conn = _connect()

        # Date filter
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days_back)
        cutoff_ns = datetime_to_apple_messages_ns(cutoff)

        # We need to fetch and decode attributedBody, so grab all recent messages
        # and filter in Python (SQLite can't search inside blobs)
        sql = """
            SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, m.date,
                   m.associated_message_type,
                   h.id as handle_id
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.date > ?
            ORDER BY m.date DESC
        """
        rows = conn.execute(sql, (cutoff_ns,)).fetchall()
        conn.close()

        query_lower = query.lower()
        contact_lower = contact.lower() if contact else ""
        matches = []

        for row in rows:
            if len(matches) >= limit:
                break

            # Filter by contact if specified
            if contact_lower:
                handle = (row["handle_id"] or "").lower()
                contact_info = resolve_handle(row["handle_id"]) if row["handle_id"] else None
                contact_name = (contact_info["name"].lower() if contact_info else "")
                if contact_lower not in handle and contact_lower not in contact_name:
                    continue

            text = extract_message_text(row["text"], row["attributedBody"])
            if text and query_lower in text.lower():
                matches.append(_format_message(row))

        if not matches:
            return f"No messages found matching '{query}'" + (
                f" from {contact}" if contact else ""
            ) + f" in the last {days_back} days."

        header = f"Found {len(matches)} message(s) matching '{query}'"
        if contact:
            header += f" from/to {contact}"
        return header + ":\n\n" + "\n".join(matches)
    except Exception as e:
        return f"Error searching messages: {e}"


def list_conversations(limit: int = 20) -> str:
    """List recent iMessage conversations.

    Args:
        limit: Maximum conversations to return (default 20)

    Returns:
        Formatted list of recent conversations with last message date
    """
    try:
        conn = _connect()
        rows = conn.execute(
            """
            SELECT c.ROWID, c.chat_identifier, c.display_name,
                   MAX(m.date) as last_msg_date, COUNT(m.ROWID) as msg_count
            FROM chat c
            JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
            JOIN message m ON cmj.message_id = m.ROWID
            GROUP BY c.ROWID
            ORDER BY last_msg_date DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()

        if not rows:
            return "No conversations found."

        results = []
        for r in rows:
            handle = r["chat_identifier"]
            display = r["display_name"] or ""
            contact = resolve_handle(handle)
            name = contact["name"] if contact else (display or handle)

            dt = apple_messages_date_to_datetime(r["last_msg_date"])
            date_str = format_datetime(dt)
            results.append(f"- {name} ({r['msg_count']} msgs, last: {date_str})")

        return f"Recent conversations ({len(rows)}):\n" + "\n".join(results)
    except Exception as e:
        return f"Error listing conversations: {e}"


def get_conversation(contact_or_chat_id: str, limit: int = 50) -> str:
    """Get messages from a specific conversation.

    Args:
        contact_or_chat_id: Contact name, phone number, email, or chat identifier
        limit: Maximum messages to return (default 50)

    Returns:
        Formatted conversation history
    """
    try:
        conn = _connect()

        # Try to find the chat by identifier directly
        chat_row = conn.execute(
            "SELECT ROWID, chat_identifier, display_name FROM chat WHERE chat_identifier LIKE ?",
            (f"%{contact_or_chat_id}%",),
        ).fetchone()

        if not chat_row:
            # Try to find by contact name via resolve
            # Get all chats and check handles
            all_chats = conn.execute(
                "SELECT ROWID, chat_identifier, display_name FROM chat"
            ).fetchall()
            for chat in all_chats:
                handle = chat["chat_identifier"]
                contact = resolve_handle(handle)
                if contact and contact_or_chat_id.lower() in contact["name"].lower():
                    chat_row = chat
                    break
                if chat["display_name"] and contact_or_chat_id.lower() in chat["display_name"].lower():
                    chat_row = chat
                    break

        if not chat_row:
            conn.close()
            return f"No conversation found for '{contact_or_chat_id}'."

        rows = conn.execute(
            """
            SELECT m.text, m.attributedBody, m.is_from_me, m.date,
                   m.associated_message_type, h.id as handle_id
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE cmj.chat_id = ?
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (chat_row["ROWID"], limit),
        ).fetchall()
        conn.close()

        if not rows:
            return f"No messages in conversation with '{contact_or_chat_id}'."

        # Reverse to show chronological order
        messages = [_format_message(row) for row in reversed(rows)]

        contact = resolve_handle(chat_row["chat_identifier"])
        chat_name = (
            contact["name"]
            if contact
            else (chat_row["display_name"] or chat_row["chat_identifier"])
        )
        return f"Conversation with {chat_name} (last {len(messages)} messages):\n\n" + "\n".join(
            messages
        )
    except Exception as e:
        return f"Error getting conversation: {e}"


def find_links(contact: str = "", days_back: int = 30) -> str:
    """Find URLs shared in messages.

    Args:
        contact: Optional contact name or handle to filter by
        days_back: How many days back to search (default 30)

    Returns:
        Formatted list of URLs found in messages
    """
    try:
        conn = _connect()
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days_back)
        cutoff_ns = datetime_to_apple_messages_ns(cutoff)

        rows = conn.execute(
            """
            SELECT m.text, m.attributedBody, m.is_from_me, m.date, h.id as handle_id
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.date > ?
            ORDER BY m.date DESC
            """,
            (cutoff_ns,),
        ).fetchall()
        conn.close()

        url_pattern = re.compile(r"https?://[^\s<>\"']+")
        contact_lower = contact.lower() if contact else ""
        links: list[str] = []

        for row in rows:
            if contact_lower:
                handle = (row["handle_id"] or "").lower()
                contact_info = resolve_handle(row["handle_id"]) if row["handle_id"] else None
                contact_name = contact_info["name"].lower() if contact_info else ""
                if contact_lower not in handle and contact_lower not in contact_name:
                    continue

            text = extract_message_text(row["text"], row["attributedBody"])
            if text:
                urls = url_pattern.findall(text)
                for url in urls:
                    sender = "You" if row["is_from_me"] else _enrich_handle(row["handle_id"])
                    dt = apple_messages_date_to_datetime(row["date"])
                    links.append(f"- {url}\n  From: {sender} on {format_datetime(dt)}")

        if not links:
            return "No links found" + (f" from {contact}" if contact else "") + f" in the last {days_back} days."

        return f"Found {len(links)} link(s):\n" + "\n".join(links)
    except Exception as e:
        return f"Error finding links: {e}"


def get_unread_count() -> str:
    """Get count of unread iMessages.

    Returns:
        Unread message count
    """
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM message WHERE is_read = 0 AND is_from_me = 0"
        ).fetchone()
        conn.close()
        count = row["cnt"] if row else 0
        return f"You have {count} unread message(s)."
    except Exception as e:
        return f"Error getting unread count: {e}"


def has_existing_conversation(target: str) -> bool:
    """True if there is already a chat with this phone number or email."""
    conn = _connect()
    try:
        identifiers = [r[0] for r in conn.execute("SELECT chat_identifier FROM chat")]
    finally:
        conn.close()
    if "@" in target:
        return any((ident or "").lower() == target.lower() for ident in identifiers)
    wanted = normalize_phone(target)
    return bool(wanted) and any(
        ident and "@" not in ident and normalize_phone(ident) == wanted for ident in identifiers
    )


def send_message(contact_or_phone: str, message: str) -> str:
    """Send an iMessage via AppleScript.

    Resolves contact names to phone numbers using the Contacts database.
    Falls back to using the input directly if it looks like a phone number or email.

    Args:
        contact_or_phone: Contact name, phone number, or email address
        message: The message text to send

    Returns:
        Confirmation or error message
    """
    if not message.strip():
        return "Error: message cannot be empty."

    # Determine the target handle (phone/email)
    target = contact_or_phone.strip()

    # If it doesn't look like a phone number or email, try to resolve as contact name
    if not re.match(r"^[+\d\s\-()]+$", target) and "@" not in target:
        contact_info = get_contact(target)
        if "Error" in contact_info or "not found" in contact_info.lower():
            return f"Error: Could not resolve contact '{target}'. Provide a phone number or email instead."

        # Extract phone number from contact info
        phone_match = re.search(r"Phone[^:]*:\s*([+\d\s\-()]+)", contact_info)
        if phone_match:
            target = re.sub(r"[\s\-()]", "", phone_match.group(1))
        else:
            # Try email
            email_match = re.search(r"Email[^:]*:\s*(\S+@\S+)", contact_info)
            if email_match:
                target = email_match.group(1)
            else:
                return f"Error: Contact '{contact_or_phone}' found but has no phone or email."

    # Limit blast radius if the agent was prompt-injected: by default only
    # reply to people you already talk to, never to a brand-new number.
    if not ALLOW_NEW_RECIPIENTS:
        try:
            known = has_existing_conversation(target)
        except Exception as e:
            return f"Error checking recipient: {e}"
        if not known:
            return (
                f"Refused: no existing conversation with {target}. Sending to new "
                "recipients is disabled (set MAC_MCP_ALLOW_NEW_RECIPIENTS=1 to allow)."
            )

    # Disclose that the message was written with AI
    message = message.rstrip() + "\n\n-sent with AI"

    escaped_message = applescript_string(message)
    escaped_target = applescript_string(target)

    applescript = f'''
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant "{escaped_target}" of targetService
        send "{escaped_message}" to targetBuddy
    end tell
    '''

    try:
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return f"Message sent to {contact_or_phone} ({target})."
        else:
            return f"Error sending message: {result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return "Error: AppleScript timed out sending message."
    except Exception as e:
        return f"Error sending message: {e}"
