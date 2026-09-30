from mrx.safety import is_protected, needs_confirmation, shell_looks_catastrophic
from pathlib import Path


def test_safe_never_confirms():
    for mode in ("strict", "balanced", "trusted"):
        assert not needs_confirmation("safe", mode, tainted=True)


def test_destructive_and_outbound_always_confirm():
    for mode in ("strict", "balanced", "trusted"):
        assert needs_confirmation("destructive", mode)
        assert needs_confirmation("outbound", mode)


def test_modes_for_modify_and_exec():
    assert needs_confirmation("modify", "strict")
    assert not needs_confirmation("modify", "balanced")
    assert not needs_confirmation("modify", "trusted")
    assert needs_confirmation("exec", "balanced")
    assert not needs_confirmation("exec", "trusted")


def test_taint_tightens_to_strict():
    assert needs_confirmation("modify", "trusted", tainted=True)
    assert needs_confirmation("exec", "trusted", tainted=True)


def test_protected_paths(home):
    assert is_protected(Path("/"))
    assert is_protected(home)
    assert is_protected(home / "Documents")
    assert not is_protected(home / "Documents" / "report.txt")
    assert not is_protected(home / "scratch")


def test_catastrophic_shell():
    assert shell_looks_catastrophic("sudo rm -rf / --no-preserve-root")
    assert not shell_looks_catastrophic("ls -la")
