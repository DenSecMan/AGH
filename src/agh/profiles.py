"""Profiles: named sets of agents, skills, tools, connectors, hooks and evals on top of default.

- default is the shipped starter and can't be changed or overwritten.
- Creating things while on default puts them in an unsaved draft; `save NAME` turns the draft into a profile.
- In a named profile, changes are saved as you go. `save OTHER` copies it to a new profile.
- Memory and other learned data are per profile (data/<profile>/). A new profile starts with a copy of the
  data of the profile it was saved from, then the two grow apart.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agh.workspace import (AGENT_DATA, BACKUPS_DIR, DATA_DIR, DEFAULT_PROFILE, DRAFT, KINDS, PROFILE_FILE,
                           PROFILES_DIR, STARTER, Workspace, WorkspaceError, read_yaml, tree_digest,
                           validate_name, write_yaml)


@dataclass
class ProfileInfo:
    name: str
    active: bool
    based_on: str
    description: str
    added: dict[str, list[str]]     # elements default doesn't have
    changed: dict[str, list[str]]   # default elements this profile replaces


def _own_elements(path: Path) -> dict[str, list[str]]:
    return {kind: sorted(p.name for p in (path / kind).iterdir() if p.is_dir() and not p.name.startswith((".", "_")))
            for kind in KINDS if (path / kind).is_dir()}


def describe(ws: Workspace, name: str) -> ProfileInfo:
    path = ws.profile_path(DRAFT if name == DEFAULT_PROFILE else name)
    meta = read_yaml(path / PROFILE_FILE) if name != DEFAULT_PROFILE else {}
    added: dict[str, list[str]] = {}
    changed: dict[str, list[str]] = {}
    for kind, names in _own_elements(path).items():
        for element in names:
            bucket = changed if (ws.default_root / kind / element).is_dir() else added
            bucket.setdefault(kind, []).append(element)
    return ProfileInfo(name=name, active=ws.profile == name, based_on=meta.get("based_on", ""),
                       description=meta.get("description", ""), added=added, changed=changed)


def list_profiles(ws: Workspace) -> list[str]:
    base = ws.root / PROFILES_DIR
    others = sorted(p.name for p in base.iterdir()
                    if p.is_dir() and p.name not in (DEFAULT_PROFILE,) and not p.name.startswith(".")) if base.is_dir() else []
    return [DEFAULT_PROFILE, *others]


def has_draft(ws: Workspace) -> bool:
    draft = ws.profile_path(DRAFT)
    if not draft.is_dir():
        return False
    return any(_own_elements(draft).values()) or bool(read_yaml(draft / PROFILE_FILE).get("exclude"))


def _copy_data(ws: Workspace, source: str, target: str) -> None:
    src, dst = ws.root / DATA_DIR / source, ws.root / DATA_DIR / target
    if src.is_dir() and not dst.exists():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("memory.db-journal", "memory.db-wal"))


def save(ws: Workspace, name: str, description: str = "") -> Path:
    """Save what is active as a new profile and switch to it."""
    validate_name(name)
    if name == DEFAULT_PROFILE:
        raise WorkspaceError("default can't be overwritten; save under another name")
    target = ws.profile_path(name)
    if target.exists():
        if name == ws.profile:
            raise WorkspaceError(f"{name} is the active profile; its changes are already saved as you go")
        raise WorkspaceError(f"profile {name} already exists; pick another name or delete it first")
    source = ws.profile
    if source == DEFAULT_PROFILE:
        draft = ws.profile_path(DRAFT)
        if draft.is_dir():
            shutil.move(str(draft), str(target))
        else:
            target.mkdir(parents=True)
    else:
        shutil.copytree(ws.overlay, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    meta = {**read_yaml(target / PROFILE_FILE), "name": name, "based_on": source, "created": time.strftime("%Y-%m-%d %H:%M")}
    if description:
        meta["description"] = description
    write_yaml(target / PROFILE_FILE, meta)
    _copy_data(ws, source, name)
    _rekey_releases(ws, ws.profile_path(DRAFT if source == DEFAULT_PROFILE else source), target)
    switch(ws, name)
    return target


def switch(ws: Workspace, name: str) -> None:
    if name != DEFAULT_PROFILE and not ws.profile_path(name).is_dir():
        raise WorkspaceError(f"no profile {name!r}; have: {', '.join(list_profiles(ws))}")
    ws.set_config("profile", name)


def delete(ws: Workspace, name: str) -> Path:
    """Move a profile and its data to the backups folder."""
    if name == DEFAULT_PROFILE:
        raise WorkspaceError("default can't be deleted")
    if name == ws.profile:
        raise WorkspaceError(f"{name} is active; switch to another profile first")
    path = ws.profile_path(validate_name(name))
    if not path.is_dir():
        raise WorkspaceError(f"no profile {name!r}")
    backup = ws.root / BACKUPS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-profile-{name}"
    backup.mkdir(parents=True)
    shutil.move(str(path), str(backup / "profile"))
    if (ws.root / DATA_DIR / name).is_dir():
        shutil.move(str(ws.root / DATA_DIR / name), str(backup / "data"))
    return backup


def discard_draft(ws: Workspace) -> list[str]:
    """Throw away unsaved changes made on default (moved to backups, not deleted)."""
    draft = ws.profile_path(DRAFT)
    if not has_draft(ws):
        return []
    names = [f"{k}/{n}" for k, ns in _own_elements(draft).items() for n in ns]
    backup = ws.root / BACKUPS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-draft"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(draft), str(backup))
    return names


def _rekey_releases(ws: Workspace, old: Path, new: Path) -> None:
    """Release records are keyed by path; moving or copying a profile carries its records along."""
    import json

    from agh.release import MANIFEST

    path = ws.state / MANIFEST
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    old_key = old.resolve().relative_to(ws.root).as_posix() + "/"
    new_key = new.resolve().relative_to(ws.root).as_posix() + "/"
    for key in [k for k in data if k.startswith(old_key)]:
        data[new_key + key[len(old_key):]] = data[key]
    path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")


# ---- migration from the flat layout --------------------------------------------------------------

def migrate_flat_layout(ws: Workspace) -> str:
    """Older workspaces kept every element at the root. Elements identical to the starter are dropped (default
    has them); anything new or changed becomes the profile `custom`. Memory, eval results and feedback move to
    data/. The old folders are kept in a backup first. Returns the profile now active."""
    root = ws.root
    backup = root / BACKUPS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-before-profiles"
    backup.mkdir(parents=True, exist_ok=True)
    custom = root / PROFILES_DIR / "custom"
    data_name = DEFAULT_PROFILE
    kept: list[tuple[str, Path]] = []
    for kind in KINDS:
        if not (root / kind).is_dir():
            continue
        shutil.copytree(root / kind, backup / kind, ignore=shutil.ignore_patterns("__pycache__"))
        for element in sorted(p for p in (root / kind).iterdir() if p.is_dir()):
            shipped = STARTER / kind / element.name
            same = shipped.is_dir() and tree_digest(element, skip=(*AGENT_DATA, "versions", "results", "baseline.json")) \
                == tree_digest(shipped, skip=(*AGENT_DATA, "versions"))
            if not same:
                kept.append((kind, element))
    if kept:
        data_name = "custom"
    data_root = root / DATA_DIR / data_name
    for kind in KINDS:
        if not (root / kind).is_dir():
            continue
        for element in (p for p in (root / kind).iterdir() if p.is_dir()):
            if kind == "agents":
                for part in AGENT_DATA:
                    if (element / part).is_dir():
                        dest = data_root / part / element.name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(element / part), str(dest))
            if kind == "evals":
                dest = data_root / "evals" / element.name
                for part in ("results", "baseline.json"):
                    if (element / part).exists():
                        dest.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(element / part), str(dest / part))
    for kind, element in kept:
        (custom / kind).mkdir(parents=True, exist_ok=True)
        shutil.move(str(element), str(custom / kind / element.name))
    for kind in KINDS:
        if (root / kind).is_dir():
            shutil.rmtree(root / kind)
    if kept:
        write_yaml(custom / PROFILE_FILE, {"name": "custom", "based_on": DEFAULT_PROFILE,
                                           "description": "What this workspace had beyond the starter before profiles"})
        _rekey_flat_releases(ws, kept, custom)
        ws.set_config("profile", "custom")
    return data_name


def _rekey_flat_releases(ws: Workspace, kept: list[tuple[str, Path]], custom: Path) -> None:
    import json

    from agh.release import MANIFEST

    path = ws.state / MANIFEST
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for kind, element in kept:
        old = f"{kind}/{element.name}"
        if old in data:
            data[f"{PROFILES_DIR}/custom/{kind}/{element.name}"] = data.pop(old)
    path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")


def summary_rows(ws: Workspace) -> list[list[Any]]:
    rows = []
    for name in list_profiles(ws):
        info = describe(ws, name)
        extra = ", ".join(f"{k}: {', '.join(v)}" for k, v in info.added.items())
        changed = ", ".join(f"{k}: {', '.join(v)}" for k, v in info.changed.items())
        parts = [p for p in (f"adds {extra}" if extra else "", f"changes {changed}" if changed else "") if p]
        if name == DEFAULT_PROFILE:
            parts = ["the starter, as installed"] + ([f"unsaved: {'; '.join(parts)}"] if parts else [])
        rows.append([("* " if info.active else "") + name, info.description or "", "; ".join(parts) or "nothing yet"])
    return rows
