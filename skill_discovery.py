"""Skill discovery: do this worker's skills resolve where pi looks for them?

pi does not inherit the parent's skills, and a bare name resolves only under
pi's discovery roots; a path outside them still works, but only when the
contract quotes it verbatim, so the launch warns instead of silently failing.
"""

import os


def skill_discoverable(skill_path, cwd):
    """True if skill_path's realpath lies under a .agents/skills or .pi/skills
    dir at cwd or any ancestor of cwd (pi's discovery roots)."""
    real = os.path.realpath(skill_path)
    root = os.path.realpath(cwd)
    while True:
        for sub in (".agents/skills", ".pi/skills"):
            base = os.path.join(root, sub)
            if os.path.isdir(base) and (real == base or real.startswith(base + os.sep)):
                return True
        parent = os.path.dirname(root)
        if parent == root:
            return False
        root = parent


def resolve_skill_name(name, cwd):
    """Resolve a bare skill NAME to a directory path, or None.

    Search order, first hit wins: .agents/skills and .pi/skills at cwd and
    every ancestor (pi's discovery roots, nearest first), then the global
    ~/.agents/skills, then ~/.hermes/skills/<category>/ (sorted), then
    ~/.pi/skills. Path-form entries never reach this function.
    """
    home = os.path.expanduser("~")
    root = os.path.realpath(cwd)
    while True:
        for sub in (".agents/skills", ".pi/skills"):
            cand = os.path.join(root, sub, name)
            if os.path.isdir(cand):
                return cand
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    cand = os.path.join(home, ".agents", "skills", name)
    if os.path.isdir(cand):
        return cand
    base = os.path.join(home, ".hermes", "skills")
    if os.path.isdir(base):
        cand = os.path.join(base, name)
        if os.path.isdir(cand):
            return cand
        for cat in sorted(os.listdir(base)):
            cand = os.path.join(base, cat, name)
            if os.path.isdir(cand):
                return cand
    cand = os.path.join(home, ".pi", "skills", name)
    if os.path.isdir(cand):
        return cand
    return None
