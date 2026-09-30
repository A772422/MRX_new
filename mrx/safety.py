"""Action gating: decides which tool calls need the user's explicit approval.

Risk levels (declared per tool):
  safe         read-only, no side effects
  modify       changes local state that is easy to undo (create/edit/copy/move files, open apps)
  exec         runs arbitrary code or drives the desktop/browser (shell, keyboard, page clicks)
  destructive  deletes data or kills processes
  outbound     leaves the machine on the user's behalf (send email, post, message)

`destructive` and `outbound` ALWAYS need approval. The autonomy mode decides the rest.
If the turn has read untrusted content (web pages, emails, news, social feeds) the gate tightens to
"strict" for the rest of the turn, so injected instructions cannot trigger silent side effects.
"""
from __future__ import annotations

import os
from pathlib import Path

RISKS = ("safe", "modify", "exec", "destructive", "outbound")

_NEEDS_CONFIRM = {
    "strict": {"modify", "exec", "destructive", "outbound"},
    "balanced": {"exec", "destructive", "outbound"},
    "trusted": {"destructive", "outbound"},
}


def needs_confirmation(risk: str, autonomy: str, tainted: bool = False) -> bool:
    if risk == "safe":
        return False
    if risk in ("destructive", "outbound"):
        return True
    mode = "strict" if tainted else autonomy
    return risk in _NEEDS_CONFIRM[mode]


def protected_paths() -> list[Path]:
    """Locations that must never be deleted/overwritten wholesale, regardless of approval."""
    home = Path.home().resolve()
    paths = [Path("/"), home, home.parent]
    for sub in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", ".ssh", ".gnupg", ".config", ".mrx"):
        paths.append(home / sub)
    if os.name == "nt":
        paths += [Path(os.environ.get("SystemRoot", r"C:\Windows")), Path(os.environ.get("ProgramFiles", r"C:\Program Files"))]
    else:
        for p in ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/opt", "/proc", "/root", "/sbin", "/sys", "/usr", "/var", "/home", "/Users", "/System", "/Library", "/Applications"):
            paths.append(Path(p))
    return paths


def is_protected(path: Path) -> bool:
    """True for a protected directory itself (deleting a *child* of it is fine)."""
    try:
        resolved = path.resolve()
    except OSError:
        return True
    return any(resolved == p for p in protected_paths())


DANGEROUS_SHELL_HINTS = ("rm -rf /", "mkfs", ":(){", "dd if=", "> /dev/sd", "chmod -R 777 /", "format c:", "del /f /s /q c:")


def shell_looks_catastrophic(cmd: str) -> bool:
    low = cmd.lower()
    return any(h in low for h in DANGEROUS_SHELL_HINTS)
