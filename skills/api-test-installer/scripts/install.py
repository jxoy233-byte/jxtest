#!/usr/bin/env python3
"""jxtest install / uninstall — set up Claude Code skill symlinks.

When `jxtest` is installed via pip or cloned from source, the user often wants
to expose its sub-skills to Claude Code so a single slash-command (or auto-
trigger) can run e.g. `jxtest run` without typing the full path.

Claude Code reads skills from `~/.claude/skills/<name>/SKILL.md`. We just need
a symlink per skill directory + a `SKILL.md` (already present in each skill
directory inside this repo).

Usage:
  jxtest install                # symlink all 17 skills to ~/.claude/skills/
  jxtest install --dry-run      # show what would happen, don't modify
  jxtest uninstall              # remove jxtest's symlinks from ~/.claude/skills/

The installer is self-bootstrap: it works whether invoked via `bin/jxtest`
or directly (e.g. `python3 install.py install`).
"""
import argparse
import os
import sys
from pathlib import Path

# Self-bootstrap for direct invocation
_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent.parent))


# SKILLS_ROOT = .../jxtest/skills. install.py lives at
# skills/api-test-installer/scripts/install.py, so go up 3 levels to reach skills/.
SKILLS_ROOT = _THIS.parent.parent.parent  # .../jxtest/skills

# Skill directories that should be exposed to Claude Code. Anything else in
# skills/ (e.g. _common, api-test-installer itself) is internal infrastructure
# and shouldn't show up as a slash-command.
INSTALLABLE_SKILLS = [
    "api-test-schema", "api-test-gen", "api-test-env", "api-test-mock",
    "api-test-run", "api-test-load", "api-test-heal", "api-test-report",
    "api-test-doc", "api-test-security", "api-test-diff", "api-test-coverage",
    "api-test-completion", "api-test-scenario", "api-test-factory",
    "api-test-suite", "api-test-doctor",
]

CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"


def cmd_install(args: argparse.Namespace) -> None:
    """Symlink each installable skill into ~/.claude/skills/."""
    if not CLAUDE_SKILLS_DIR.exists():
        if args.dry_run:
            print(f"[dry-run] would create {CLAUDE_SKILLS_DIR}", file=sys.stderr)
        else:
            CLAUDE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
            print(f"  ✓ created {CLAUDE_SKILLS_DIR}", file=sys.stderr)

    created: list[str] = []
    skipped_existing: list[str] = []
    replaced: list[str] = []

    for name in INSTALLABLE_SKILLS:
        src = SKILLS_ROOT / name
        if not src.exists():
            print(f"  ✗ {name}: not found at {src}", file=sys.stderr)
            continue
        dst = CLAUDE_SKILLS_DIR / name
        if dst.exists() or dst.is_symlink():
            if dst.is_symlink() and dst.resolve() == src.resolve():
                skipped_existing.append(name)
                continue
            # A real (non-symlink) directory exists. Don't blindly rm — the
            # user might have something there. Either replace (--force) or warn.
            if args.force:
                if args.dry_run:
                    print(f"[dry-run] would replace {dst} → {src}", file=sys.stderr)
                else:
                    if dst.is_symlink():
                        dst.unlink()
                    else:
                        import shutil
                        shutil.rmtree(dst)
                    dst.symlink_to(src)
                    replaced.append(name)
            else:
                print(f"  ⚠ {name}: {dst} already exists (not a symlink). "
                      f"Pass --force to replace.", file=sys.stderr)
            continue
        if args.dry_run:
            print(f"[dry-run] would symlink {dst} → {src}", file=sys.stderr)
        else:
            dst.symlink_to(src)
            created.append(name)

    print("", file=sys.stderr)
    print(f"jxtest install: {len(created)} created, {len(replaced)} replaced, "
          f"{len(skipped_existing)} already correct", file=sys.stderr)
    if not args.dry_run:
        if created:
            print(f"  ✓ created: {', '.join(created)}", file=sys.stderr)
        if replaced:
            print(f"  ⟳ replaced: {', '.join(replaced)}", file=sys.stderr)
        if skipped_existing:
            print(f"  · already installed: {len(skipped_existing)} skills "
                  f"(use --force to re-link)", file=sys.stderr)
    if created or replaced:
        print(f"\n  Next: open Claude Code — skills should appear automatically. "
              f"Verify with: ls -la {CLAUDE_SKILLS_DIR} | grep jxtest", file=sys.stderr)


def cmd_uninstall(args: argparse.Namespace) -> None:
    """Remove jxtest's symlinks from ~/.claude/skills/."""
    removed: list[str] = []
    not_jxtest: list[str] = []

    for name in INSTALLABLE_SKILLS:
        dst = CLAUDE_SKILLS_DIR / name
        if not dst.exists() and not dst.is_symlink():
            continue
        if dst.is_symlink():
            target = dst.resolve()
            if SKILLS_ROOT in target.parents or target == (SKILLS_ROOT / name).resolve():
                if args.dry_run:
                    print(f"[dry-run] would unlink {dst}", file=sys.stderr)
                else:
                    dst.unlink()
                    removed.append(name)
            else:
                not_jxtest.append(f"{name} → {target}")
        else:
            not_jxtest.append(f"{name} (not a symlink)")

    print("", file=sys.stderr)
    # Count what would have been removed in dry-run mode (the loop printed
    # "[dry-run] would unlink" lines without populating `removed`).
    would_remove = sum(1 for name in INSTALLABLE_SKILLS
                       if (CLAUDE_SKILLS_DIR / name).is_symlink()
                       and ((CLAUDE_SKILLS_DIR / name).resolve() in
                            {(SKILLS_ROOT / name).resolve() for name in INSTALLABLE_SKILLS}))
    if removed:
        print(f"jxtest uninstall: removed {len(removed)} symlinks", file=sys.stderr)
        for n in removed:
            print(f"  ✓ {n}", file=sys.stderr)
    elif args.dry_run and would_remove:
        print(f"[dry-run] {would_remove} symlinks would be removed "
              f"(re-run without --dry-run to actually remove)", file=sys.stderr)
    else:
        print("jxtest uninstall: nothing to remove", file=sys.stderr)
    if not_jxtest:
        print(f"\n  Skipped (not jxtest symlinks):", file=sys.stderr)
        for n in not_jxtest:
            print(f"    {n}", file=sys.stderr)


def main() -> None:
    # When invoked via `bin/jxtest install` (no subcommand), default to install.
    # When invoked directly (`python3 install.py ...`), argparse handles the
    # required subcommand as usual. Strip the auto-injected default back out
    # when the user passed one explicitly.
    raw = sys.argv[1:]
    if raw and raw[0] not in ("install", "uninstall", "-h", "--help"):
        sys.argv = [sys.argv[0], "install", *raw]

    ap = argparse.ArgumentParser(description="Install/uninstall jxtest Claude Code skills")
    sub = ap.add_subparsers(dest="cmd")

    p_install = sub.add_parser("install", help="Symlink skills into ~/.claude/skills/")
    p_install.add_argument("--dry-run", action="store_true",
                           help="Show what would happen without modifying anything")
    p_install.add_argument("--force", action="store_true",
                           help="Replace existing non-symlink dirs at the target paths")

    p_uninstall = sub.add_parser("uninstall", help="Remove jxtest symlinks from ~/.claude/skills/")
    p_uninstall.add_argument("--dry-run", action="store_true",
                             help="Show what would happen without modifying anything")

    args = ap.parse_args()
    {"install": cmd_install, "uninstall": cmd_uninstall}[args.cmd](args)


if __name__ == "__main__":
    main()