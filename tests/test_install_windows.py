"""Drive the Windows branches of install.py on a non-Windows host.

Run with: python3 tests/test_install_windows.py

This exercises the decision logic (shim content, marker protection, junction
fallback, uninstall scoping) — not the OS behaviour itself. Junction creation
and Windows symlink privilege rules still need a real Windows box to confirm.

It earns its keep: it caught two Windows-only bugs that never reproduce on
POSIX, both from text-mode newline translation. `write_text` re-translates the
shim's CRLF into `\\r\\r\\n` on Windows, and `read_text` normalises it back on
read — which also broke the idempotency check, so every install rewrote the
shim and reported "refreshed". Neither is visible unless you assert on bytes.
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "skills" / "api-test-installer" / "scripts"))
import install  # noqa: E402

FAILS = []


def check(label, cond, detail=""):
    print(f"{'✓' if cond else '✗'} {label}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


args = argparse.Namespace(dry_run=False, force=False)
dry = argparse.Namespace(dry_run=True, force=False)
forced = argparse.Namespace(dry_run=False, force=True)

with tempfile.TemporaryDirectory() as td:
    scripts = Path(td) / "Scripts"
    scripts.mkdir()
    shim = scripts / "jxtest.cmd"

    with mock.patch.object(install, "IS_WINDOWS", True), \
         mock.patch.object(install, "cli_target", lambda: shim):

        # --- shim generation -------------------------------------------------
        install._install_cli_windows(args)
        check("shim created", shim.exists())
        raw = shim.read_bytes()
        body = raw.decode("utf-8")
        check("shim has marker", install.SHIM_MARKER in body)
        check("shim pins interpreter", sys.executable in body)
        check("shim points at bin/jxtest", str(install.JXTEST_BIN) in body)
        check("shim forwards args", "%*" in body)
        check("shim uses CRLF on disk", b"\r\n" in raw and b"\r\r\n" not in raw)
        check("shim is not a copy of the python source",
              "#!/usr/bin/env python3" not in body)

        # --- idempotency -----------------------------------------------------
        import io
        cap = io.StringIO()
        with mock.patch("sys.stderr", cap):
            install._install_cli_windows(args)
        check("second run is a no-op", "already current" in cap.getvalue(),
              cap.getvalue().strip())

        # --- refuses to clobber a foreign file --------------------------------
        shim.write_text("echo someone elses script\r\n")
        cap = io.StringIO()
        with mock.patch("sys.stderr", cap):
            install._install_cli_windows(args)
        check("foreign file preserved without --force",
              shim.read_text().startswith("echo someone"))
        check("foreign file warns about --force", "--force" in cap.getvalue())

        install._install_cli_windows(forced)
        check("--force replaces foreign file", install.SHIM_MARKER in shim.read_text())

        # --- uninstall scoping ------------------------------------------------
        install._uninstall_cli(dry)
        check("uninstall --dry-run keeps the file", shim.exists())
        install._uninstall_cli(args)
        check("uninstall removes our shim", not shim.exists())

        shim.write_text("echo someone elses script\r\n")
        install._uninstall_cli(args)
        check("uninstall leaves foreign file alone", shim.exists())

        # --- PATH guidance is Windows-flavoured -------------------------------
        cap = io.StringIO()
        with mock.patch("sys.stderr", cap), \
             mock.patch.dict(os.environ, {"PATH": "/somewhere/else"}, clear=False):
            install._warn_if_not_on_path()
        out = cap.getvalue()
        check("PATH hint uses setx/PowerShell", "setx" in out and "$env:Path" in out)
        check("PATH hint avoids the zsh advice", "zshrc" not in out)

    # --- junction fallback ---------------------------------------------------
    src = Path(td) / "skill-src"
    src.mkdir()
    dst = Path(td) / "skill-dst"
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        os.symlink(src, dst)  # stand in for what mklink /J would do
        return mock.Mock(returncode=0, stderr="")

    with mock.patch.object(install, "IS_WINDOWS", True), \
         mock.patch.object(Path, "symlink_to", side_effect=OSError("WinError 1314")), \
         mock.patch.object(install.subprocess, "run", fake_run):
        mech = install._make_dir_link(src, dst)
    check("falls back to junction when symlink is refused", mech == "junction", mech)
    check("invokes mklink /J", calls and calls[0][:4] == ["cmd", "/c", "mklink", "/J"],
          str(calls))

    # --- both mechanisms refused -> OSError, not a traceback -----------------
    dst2 = Path(td) / "skill-dst2"
    with mock.patch.object(install, "IS_WINDOWS", True), \
         mock.patch.object(Path, "symlink_to", side_effect=OSError("WinError 1314")), \
         mock.patch.object(install.subprocess, "run",
                           lambda *a, **k: mock.Mock(returncode=1, stderr="Access denied")):
        try:
            install._make_dir_link(src, dst2)
            check("raises OSError when both fail", False, "no exception")
        except OSError as e:
            check("raises OSError when both fail", "Access denied" in str(e), str(e))

print()
print(f"{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all Windows-branch checks passed'}")
sys.exit(1 if FAILS else 0)
