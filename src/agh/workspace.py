"""A workspace is a directory holding agh.yaml, profiles of elements, and the data agents build up.

    agh.yaml                 settings, including `profile:` (the active one)
    AGENTS.md                instructions for every agent in every profile (optional)
    profiles/
      default/               the shipped starter, kept identical to a fresh install; nothing writes here
      .draft/                changes made while on default, until `/profile save NAME`
      <name>/                profile.yaml plus only the elements this profile adds or changes
    data/<profile>/          memory, eval results, feedback and drafts, per profile
    state/                   sessions, traces, spilled output, checkpoints (shared)

An element (agent, skill, tool, connector, hook or eval) resolves in the active profile first, then in default.
Writes go to the active profile; changing a default element there copies it in first.
"""
from __future__ import annotations

import functools
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import yaml

CONFIG_NAME = "agh.yaml"
BACKUPS_DIR = ".agh-backups"
STARTER = Path(__file__).parent / "starter"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
KINDS = ("agents", "skills", "tools", "connectors", "hooks", "evals")
PROFILES_DIR = "profiles"
DATA_DIR = "data"
DEFAULT_PROFILE = "default"
DRAFT = ".draft"
BUNDLED_RECORD = "bundled_profiles.yaml"  # in state/: shipped profiles already installed, so a deleted one stays gone
PROFILE_FILE = "profile.yaml"
FLAT_MARKER = ".agh-flat"  # a staged copy for release tests: elements sit at the root, no profiles
AGENT_DATA = ("memory", "improvements")  # what an agent accumulates; kept under data/, not in its definition
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


class WorkspaceError(RuntimeError):
    pass


def validate_name(name: str) -> str:
    """Element names become directory names, so this also blocks path traversal."""
    if not NAME_RE.match(name or ""):
        raise WorkspaceError(
            f"invalid name {name!r}: use lowercase letters, digits, _ or -, starting with a letter"
        )
    return name


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def load_dotenv(path: Path) -> None:
    from agh.keys import load_env_file

    load_env_file(path)


def tree_digest(directory: Path, skip: tuple[str, ...] = ()) -> str:
    import hashlib

    sha = hashlib.sha256()
    for path in sorted(p for p in Path(directory).rglob("*") if p.is_file()):
        rel = path.relative_to(directory).as_posix()
        if "__pycache__" in rel or rel.endswith(".pyc") or any(rel.startswith(s) or f"/{s}" in rel for s in skip):
            continue
        sha.update(rel.encode() + b"\0" + path.read_bytes() + b"\0")
    return sha.hexdigest()


@functools.lru_cache(maxsize=1)
def _starter_digest() -> str:
    return tree_digest(STARTER)


def _copy_starter_elements(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        if (STARTER / kind).is_dir():
            shutil.copytree(STARTER / kind, target / kind, ignore=IGNORE)


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        # the folder agh was started in: the Explorer shows it, /init and AGENTS.md belong to it
        self.project: Path = getattr(self, "project", None) or self.root
        self.config = read_yaml(self.root / CONFIG_NAME)
        self.load_errors: dict[str, str] = {}
        from agh.keys import reload

        reload(self.root)  # user-wide ~/.agh/.env first, then this workspace's .env
        for sub in ("sessions", "traces", "outputs"):
            (self.state / sub).mkdir(parents=True, exist_ok=True)
        self.flat = (self.root / FLAT_MARKER).exists()
        if not self.flat:
            if (self.root / "agents").is_dir() and not (self.root / PROFILES_DIR).is_dir():
                from agh.profiles import migrate_flat_layout

                migrate_flat_layout(self)
            self.sync_default()
            self.install_bundled_profiles()
            if not self.profile_path(self.profile).is_dir() and self.profile != DEFAULT_PROFILE:
                self.load_errors["profile"] = f"profile {self.profile!r} is missing; using default"
                self.config["profile"] = DEFAULT_PROFILE

    # ---- layout ------------------------------------------------------------------------------

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def profile(self) -> str:
        return str(self.config.get("profile") or DEFAULT_PROFILE)

    def profile_path(self, name: str) -> Path:
        return self.root / PROFILES_DIR / name

    @property
    def default_root(self) -> Path:
        return self.profile_path(DEFAULT_PROFILE)

    @property
    def overlay(self) -> Path:
        """Where this profile's own elements live and where every write goes."""
        if self.flat:
            return self.root
        return self.profile_path(DRAFT if self.profile == DEFAULT_PROFILE else self.profile)

    @property
    def layers(self) -> list[Path]:
        return [self.root] if self.flat else [self.overlay, self.default_root]

    @property
    def profile_meta(self) -> dict[str, Any]:
        # The draft of default has a profile.yaml only when it hides default elements (see remove_element).
        return {} if self.flat else read_yaml(self.overlay / PROFILE_FILE)

    @property
    def default_agent(self) -> str:
        return str(self.profile_meta.get("default_agent") or self.config.get("default_agent") or "assistant")

    def sync_default(self) -> bool:
        """Make profiles/default identical to the shipped starter. True if it had to be (re)written."""
        target = self.default_root
        if target.is_dir() and tree_digest(target) == _starter_elements_digest():
            return False
        if target.exists():
            shutil.rmtree(target)
        _copy_starter_elements(target)
        return True

    def install_bundled_profiles(self) -> list[str]:
        """Copy profiles shipped in the starter (starter/profiles/<name>) into this workspace, once each. After
        that they belong to the user: changes are kept, and a deleted one stays deleted. Returns names installed."""
        shipped = STARTER / PROFILES_DIR
        if not shipped.is_dir():
            return []
        record = self.state / BUNDLED_RECORD
        seen = set(read_yaml(record).get("installed") or []) if record.exists() else set()
        installed = []
        for source in sorted(p for p in shipped.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
            if source.name in seen:
                continue
            if not self.profile_path(source.name).exists():
                shutil.copytree(source, self.profile_path(source.name), ignore=IGNORE)
                installed.append(source.name)
            seen.add(source.name)
        if installed or not record.exists():
            write_yaml(record, {"installed": sorted(seen)})
        return installed

    def dir(self, kind: str) -> Path:
        """The active profile's directory for a kind: where new elements are written. Created on first use."""
        path = self.overlay / kind
        path.mkdir(parents=True, exist_ok=True)
        return path

    def element(self, kind: str, name: str) -> Path:
        """Where an element is found: the active profile first, then default. A missing element gets the
        path it would be created at, so callers can test .exists() and report clearly."""
        hidden = name in self.excluded(kind)
        for layer in self.layers:
            path = layer / kind / name
            if path.is_dir() and not (hidden and layer == self.default_root):
                return path
        return self.dir(kind) / name

    def excluded(self, kind: str) -> set[str]:
        hidden = (self.profile_meta.get("exclude") or {})
        return set(hidden.get(kind) or []) if isinstance(hidden, dict) else set()

    def _set_excluded(self, kind: str, names: set[str]) -> None:
        meta = read_yaml(self.overlay / PROFILE_FILE)
        exclude = meta.get("exclude") if isinstance(meta.get("exclude"), dict) else {}
        if names:
            exclude[kind] = sorted(names)
        else:
            exclude.pop(kind, None)
        if exclude:
            meta["exclude"] = exclude
        else:
            meta.pop("exclude", None)
        self.overlay.mkdir(parents=True, exist_ok=True)
        if meta:
            write_yaml(self.overlay / PROFILE_FILE, meta)
        elif (self.overlay / PROFILE_FILE).exists():
            (self.overlay / PROFILE_FILE).unlink()

    def remove_element(self, kind: str, name: str) -> Path | None:
        """Remove an element from the active profile. Its own copy moves to backups (returned); one that comes
        from default is hidden in this profile instead, since default is never written to."""
        if self.flat:
            raise WorkspaceError("elements can't be removed from a flat staging copy")
        if name not in self.names(kind):
            raise WorkspaceError(f"no {kind[:-1]} named {name!r} in profile {self.profile}")
        own = self.overlay / kind / name
        backup = None
        if own.is_dir():
            backup = self.root / BACKUPS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{name}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(own), str(backup))
        if (self.default_root / kind / name).is_dir():
            self._set_excluded(kind, self.excluded(kind) | {name})
        return backup

    def restore_element(self, kind: str, name: str) -> None:
        """Bring back a default element hidden in this profile by remove_element."""
        if name not in self.excluded(kind):
            raise WorkspaceError(f"{kind[:-1]} {name!r} is not hidden in profile {self.profile}")
        self._set_excluded(kind, self.excluded(kind) - {name})

    def names(self, kind: str) -> list[str]:
        found: set[str] = set()
        for layer in self.layers:
            d = layer / kind
            if d.is_dir():
                found |= {p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))}
        return sorted(found - self.excluded(kind))

    def writable(self, kind: str, name: str) -> Path:
        """The element's directory in the active profile, copying it from default first if needed, so a change
        never touches default. What the agent has learned stays in data/, so it isn't copied."""
        target = self.dir(kind) / name
        if not target.exists():
            source = self.element(kind, name)
            if source != target and source.is_dir():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", *AGENT_DATA))
        return target

    def is_default(self, path: Path) -> bool:
        return not self.flat and Path(path).resolve().is_relative_to(self.default_root.resolve())

    # ---- per-profile data --------------------------------------------------------------------

    @property
    def data_profile(self) -> str:
        return self.profile  # default and its draft share default's data

    def data_dir(self, *parts: str) -> Path:
        base = self.root / (FLAT_MARKER + "-data" if self.flat else DATA_DIR) / self.data_profile
        path = base.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def memory_dir(self, agent: str) -> Path:
        """An agent's memory in this profile. A new one starts from the core notes the agent ships with."""
        path = self.data_dir("memory", agent)
        seed = self.element("agents", agent) / "memory" / "core.md"
        if not (path / "core.md").exists() and seed.is_file():
            shutil.copy2(seed, path / "core.md")
        return path

    # ---- settings ----------------------------------------------------------------------------

    def set_config(self, key: str, value: Any) -> None:
        """Set a top-level scalar in agh.yaml, keeping comments and everything else."""
        path = self.root / CONFIG_NAME
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        line = yaml.safe_dump({key: value}, default_flow_style=True).strip().strip("{}")
        pattern = re.compile(rf"^{re.escape(key)}:.*$", re.M)
        text = pattern.sub(lambda _: line, text, count=1) if pattern.search(text) else f"{line}\n{text}"
        path.write_text(text, encoding="utf-8")
        self.config[key] = value

    def extra_paths(self) -> tuple[Path, ...]:
        extra = (self.config.get("sandbox") or {}).get("extra_paths") or []
        return tuple(Path(p).expanduser().resolve() for p in extra)

    # ---- lifecycle ---------------------------------------------------------------------------

    def reset(self) -> Path:
        """Back to a fresh install: default profile only, no memory, sessions or other profiles.

        Keeps .env, AGENTS.md, earlier backups and files the harness doesn't manage; everything replaced moves to
        a backup. Close engines first: on Windows an open memory database can't be moved. Any failure puts
        everything back.
        """
        backup = self.root / BACKUPS_DIR / time.strftime("%Y%m%d-%H%M%S")
        backup = backup.with_name(f"{backup.name}-{time.time_ns() % 1_000_000:06d}")
        backup.mkdir(parents=True)
        starter_files = {p.name for p in STARTER.iterdir() if p.is_file()} - {".env"}
        managed = starter_files | {PROFILES_DIR, DATA_DIR, "state", *KINDS}
        moved: list[str] = []
        try:
            for name in sorted(managed):
                if (self.root / name).exists():
                    shutil.move(str(self.root / name), str(backup / name))
                    moved.append(name)
            _write_fresh(self.root)
        except OSError as exc:
            for name in moved:
                target = self.root / name
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                shutil.move(str(backup / name), str(target))
            shutil.rmtree(backup, ignore_errors=True)
            raise WorkspaceError(f"reset failed, nothing changed: {exc}") from exc
        self.__init__(self.root)
        return backup

    @classmethod
    def locate(cls, start: Path | None = None) -> Path | None:
        """The nearest directory at or above `start` holding agh.yaml. The home directory and above are
        skipped, so a stray ~/agh.yaml can't capture every folder you run agh from."""
        env = os.environ.get("AGH_WORKSPACE")
        here = Path(env or start or Path.cwd()).resolve()
        home = Path.home().resolve()
        for candidate in (here, *here.parents):
            if candidate != here and (candidate == home or home.is_relative_to(candidate)):
                break
            if (candidate / CONFIG_NAME).exists():
                return candidate
        return None

    @classmethod
    def find(cls, start: Path | None = None) -> "Workspace":
        root = cls.locate(start)
        if root is None:
            here = Path(os.environ.get("AGH_WORKSPACE") or start or Path.cwd()).resolve()
            raise WorkspaceError(f"no {CONFIG_NAME} found from {here}. Run `agh init` or just `agh` there.")
        return cls(root)

    @classmethod
    def init(cls, target: Path) -> "Workspace":
        target = Path(target).resolve()
        if (target / CONFIG_NAME).exists():
            raise WorkspaceError(f"{target} is already a workspace")
        _write_fresh(target)
        return cls(target)


@functools.lru_cache(maxsize=1)
def _starter_elements_digest() -> str:
    """The digest profiles/default must have: the starter's element folders, nothing else."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        _copy_starter_elements(Path(tmp) / "d")
        return tree_digest(Path(tmp) / "d")


def _write_fresh(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for path in STARTER.iterdir():
        if path.is_file() and not (target / path.name).exists():
            shutil.copy2(path, target / path.name)
    _copy_starter_elements(target / PROFILES_DIR / DEFAULT_PROFILE)
