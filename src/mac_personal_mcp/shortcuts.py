"""Apple Shortcuts integration: list, run, and create Shortcuts programmatically."""

import json
import plistlib
import re
import subprocess
import tempfile
import uuid
from pathlib import Path


def list_shortcuts() -> str:
    """List all available Apple Shortcuts.

    Returns:
        Formatted list of shortcut names
    """
    try:
        result = subprocess.run(
            ["shortcuts", "list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return f"Error listing shortcuts: {result.stderr.strip()}"

        names = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        if not names:
            return "No shortcuts found."

        return f"Shortcuts ({len(names)}):\n" + "\n".join(f"- {n}" for n in sorted(names))
    except FileNotFoundError:
        return "Error: 'shortcuts' CLI not found. Requires macOS 12+."
    except subprocess.TimeoutExpired:
        return "Error: shortcuts list timed out after 30s."
    except Exception as e:
        return f"Error listing shortcuts: {e}"


def run_shortcut(name: str, input_text: str = "") -> str:
    """Run an Apple Shortcut by name and return its output.

    Args:
        name: Exact name of the shortcut to run
        input_text: Optional text input to pass to the shortcut

    Returns:
        Output from the shortcut, or confirmation if no output
    """
    try:
        cmd = ["shortcuts", "run", name]
        stdin_data = None

        if input_text:
            cmd.extend(["--input-type", "text"])
            stdin_data = input_text

        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "no shortcuts" in stderr.lower() or "couldn't find" in stderr.lower():
                return f"Shortcut '{name}' not found. Use shortcuts_list to see available shortcuts."
            return f"Error running '{name}': {stderr}"

        output = result.stdout.strip()
        if output:
            return output
        return f"Shortcut '{name}' completed (no output)."
    except FileNotFoundError:
        return "Error: 'shortcuts' CLI not found. Requires macOS 12+."
    except subprocess.TimeoutExpired:
        return f"Error: shortcut '{name}' timed out after 120s."
    except Exception as e:
        return f"Error running shortcut: {e}"


def create_shortcut(name: str, actions_json: str) -> str:
    """Create an Apple Shortcut from a JSON action definition, sign it, and open for import.

    The actions_json should be a JSON array of action objects, each with:
    - identifier: The action identifier (e.g. 'is.workflow.actions.gettext')
    - parameters: Dict of action parameters

    Variable references between actions use OutputUUID/OutputName.
    Text interpolation uses U+FFFC placeholder with attachmentsByRange.

    Args:
        name: Name for the shortcut
        actions_json: JSON array of action definitions

    Returns:
        Success message or error
    """
    try:
        actions = json.loads(actions_json)
        if not isinstance(actions, list):
            return "Error: actions_json must be a JSON array."
    except json.JSONDecodeError as e:
        return f"Error parsing actions JSON: {e}"

    wf_actions = []
    for action in actions:
        identifier = action.get("identifier", "")
        parameters = action.get("parameters", {})
        if "UUID" not in parameters:
            parameters["UUID"] = str(uuid.uuid4()).upper()
        wf_actions.append({
            "WFWorkflowActionIdentifier": identifier,
            "WFWorkflowActionParameters": parameters,
        })

    shortcut_plist = {
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowIcon": {
            "WFWorkflowIconStartColor": 4282601983,
            "WFWorkflowIconGlyphNumber": 59511,
        },
        "WFWorkflowClientVersion": "2302.0.4",
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowName": name,
        "WFWorkflowActions": wf_actions,
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowTypes": [],
        "WFWorkflowImportQuestions": [],
        "WFQuickActionSurfaces": [],
        "WFWorkflowHasShortcutInputVariables": False,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        unsigned_path = Path(tmpdir) / "unsigned.shortcut"
        # The file name becomes the default import name; keep it path-safe
        safe_name = re.sub(r"[^\w .-]", "_", name).strip(". ") or "Shortcut"
        signed_path = Path(tmpdir) / f"{safe_name}.shortcut"

        with open(unsigned_path, "wb") as f:
            plistlib.dump(shortcut_plist, f, fmt=plistlib.FMT_BINARY)

        result = subprocess.run(
            [
                "shortcuts", "sign",
                "--mode", "people-who-know-me",
                "--input", str(unsigned_path),
                "--output", str(signed_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if not signed_path.exists():
            return f"Error signing shortcut: {result.stderr.strip()}"

        subprocess.run(["open", str(signed_path)], timeout=10)

    return (
        f"Shortcut '{name}' created and opened for import. "
        f"Actions: {len(wf_actions)}. "
        f"Click 'Add Shortcut' in the Shortcuts app to install it."
    )
