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
    found = _skill_in_tree(name, cwd)
    if found:
        return found
    candidates = [
        os.path.join(home, ".agents", "skills", name),
        _hermes_skill(name, home),
        os.path.join(home, ".pi", "skills", name),
    ]
    for cand in candidates:
        if cand and os.path.isdir(cand):
            return cand
    return None


def _skill_in_tree(name, cwd):
    root = os.path.realpath(cwd)
    while True:
        for sub in (".agents/skills", ".pi/skills"):
            cand = os.path.join(root, sub, name)
            if os.path.isdir(cand):
                return cand
        parent = os.path.dirname(root)
        if parent == root:
            return None
        root = parent


def _hermes_skill(name, home):
    base = os.path.join(home, ".hermes", "skills")
    if not os.path.isdir(base):
        return None
    for cand in [os.path.join(base, name)] + [
        os.path.join(base, cat, name) for cat in sorted(os.listdir(base))
    ]:
        if os.path.isdir(cand):
            return cand
    return None
