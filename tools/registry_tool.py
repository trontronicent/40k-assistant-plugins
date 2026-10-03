#!/usr/bin/env python3
"""STC Index tool: validate the registry, validate a plugin, pin a plugin version.

Standard library only (Python 3.9+), so it runs in CI and on any machine without
installing anything. The same checks are repeated by the Strategicum app after
it clones a plugin; this tool exists so mistakes are caught at review time.

Commands:
  validate REGISTRY [--remote] [--only ID ...] [--allow-local]
      Check registry.json. With --remote, also check every entry's repository:
      the tag must exist and point at the pinned commit, and the manifest at
      that commit must match the entry.
  manifest DIR [--expect-id ID] [--expect-version V]
      Check a plugin checkout (strategicum-plugin.json and every declared file).
  pin REGISTRY --repo URL --tag TAG [--allow-local]
      Resolve TAG to its commit, read the manifest at that commit, and add or
      update the matching entry in registry.json.

Exit code 0 = no errors (warnings allowed), 1 = errors, 2 = usage error.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

MANIFEST_NAME = "strategicum-plugin.json"
SUPPORTED_SCHEMA_VERSION = 1
SUPPORTED_MANIFEST_VERSION = 1
SUPPORTED_PLUGIN_API = {1}
KINDS = ("themes", "personas", "codex")
MAX_TOTAL_BYTES = 50 * 1024 * 1024
GIT_TIMEOUT_S = 120

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
SUB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
APP_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TAG_NAME_RE = re.compile(r"^[a-z0-9-]{1,24}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Windows-invalid characters; a leading "_" or "." would be skipped by the Codex folder sync.
LIBRARY_RE = re.compile(r'^[^_./\\:*?"<>|][^/\\:*?"<>|]{0,79}$')
# Theme CSS must not load anything from elsewhere (a theme must not "call home").
REMOTE_CSS_RE = re.compile(r"@import|url\(\s*['\"]?\s*(?:https?:)?//", re.IGNORECASE)
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

ENTRY_REQUIRED = ("id", "name", "description", "author", "repo", "version", "tag",
                  "commit", "kinds", "min_app_version", "plugin_api", "license")
ENTRY_OPTIONAL = ("homepage", "tags", "yanked", "deprecated", "max_app_version")

# Persona fields a plugin may set; everything else is dropped by the app.
PERSONA_FIELDS = {
    "name", "appearance", "clothing", "personality", "speech_style", "background",
    "custom", "system_prompt", "system_prompt_locked", "model", "voice",
    "temperature", "repeat_penalty", "top_p", "top_k", "roleplay_helpers",
    "mnemo_vigil_threshold_hours", "web_search_mode", "knowledge_mode",
    "knowledge_top_k", "knowledge_all_libraries", "create_memories", "read_memories",
    "knowledge_plugin_libraries",
}


class Report:
    """Collects errors and warnings under a label and prints them."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"{where}: {msg}")

    def extend(self, other: "Report") -> None:
        self.errors += other.errors
        self.warnings += other.warnings

    def print(self) -> None:
        for w in self.warnings:
            print(f"WARN  {w}")
        for e in self.errors:
            print(f"ERROR {e}")
        print(f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)")


# --------------------------------------------------------------------------- registry


def _check_repo_url(url: object, allow_local: bool) -> str | None:
    """Return a problem with a clone URL, or None when it is acceptable."""
    if not isinstance(url, str):
        return "repo must be a string"
    if allow_local and url.startswith("file://"):
        return None
    if not url.startswith("https://") or not url.endswith(".git") or any(c.isspace() for c in url):
        return "repo must be an https:// clone URL ending in .git"
    return None


def validate_entry(entry: object, index: int, allow_local: bool = False) -> Report:
    """Check one registry entry's shape and values (no network)."""
    rep = Report()
    where = f"plugins[{index}]"
    if not isinstance(entry, dict):
        rep.error(where, "must be an object")
        return rep
    if isinstance(entry.get("id"), str):
        where = f"plugins[{index}] ({entry['id']})"
    for key in ENTRY_REQUIRED:
        if key not in entry:
            rep.error(where, f"missing '{key}'")
    for key in entry:
        if key not in ENTRY_REQUIRED and key not in ENTRY_OPTIONAL:
            rep.error(where, f"unknown key '{key}'")

    def is_text(key: str, max_len: int) -> bool:
        value = entry.get(key)
        if key in entry and (not isinstance(value, str) or not value.strip() or len(value) > max_len):
            rep.error(where, f"'{key}' must be a non-empty string of at most {max_len} characters")
            return False
        return True

    for key, max_len in (("name", 60), ("description", 300), ("author", 80), ("license", 60), ("tag", 100)):
        is_text(key, max_len)
    if "id" in entry and not (isinstance(entry["id"], str) and ID_RE.match(entry["id"])):
        rep.error(where, "id must match ^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
    if "repo" in entry:
        problem = _check_repo_url(entry["repo"], allow_local)
        if problem:
            rep.error(where, problem)
    if "version" in entry and not (isinstance(entry["version"], str) and SEMVER_RE.match(entry["version"])):
        rep.error(where, "version must be semantic (1.2.3)")
    if isinstance(entry.get("version"), str) and isinstance(entry.get("tag"), str) \
            and entry["tag"] not in (entry["version"], f"v{entry['version']}"):
        rep.error(where, f"tag '{entry['tag']}' must be 'v{entry['version']}' (or '{entry['version']}')")
    for key in ("min_app_version", "max_app_version"):
        if key in entry and not (isinstance(entry[key], str) and SEMVER_RE.match(entry[key])):
            rep.error(where, f"{key} must be semantic (3.0.0)")
    if "commit" in entry:
        commit = entry["commit"]
        if not (isinstance(commit, str) and COMMIT_RE.match(commit)):
            rep.error(where, "commit must be the full 40-character lowercase hash")
        elif set(commit) == {"0"}:
            rep.error(where, "commit is still the placeholder; run: registry_tool.py pin ...")
    if "kinds" in entry:
        kinds = entry["kinds"]
        if not (isinstance(kinds, list) and kinds and all(k in KINDS for k in kinds)
                and len(set(kinds)) == len(kinds)):
            rep.error(where, f"kinds must be a non-empty list of distinct values from {list(KINDS)}")
    if "plugin_api" in entry and entry["plugin_api"] not in SUPPORTED_PLUGIN_API:
        rep.error(where, f"plugin_api must be one of {sorted(SUPPORTED_PLUGIN_API)}")
    if "homepage" in entry and not (isinstance(entry["homepage"], str) and entry["homepage"].startswith("https://")):
        rep.error(where, "homepage must be an https:// URL")
    if "tags" in entry:
        tags = entry["tags"]
        if not (isinstance(tags, list) and len(tags) <= 10 and len(set(map(str, tags))) == len(tags)
                and all(isinstance(t, str) and TAG_NAME_RE.match(t) for t in tags)):
            rep.error(where, "tags must be at most 10 distinct lowercase words (a-z, 0-9, -)")
    for key in ("yanked", "deprecated"):
        is_text(key, 300)
    return rep


def validate_registry_doc(doc: object, allow_local: bool = False) -> Report:
    """Check the whole registry document (no network)."""
    rep = Report()
    if not isinstance(doc, dict):
        rep.error("registry", "must be a JSON object")
        return rep
    allowed = {"$schema", "schema_version", "name", "updated", "plugins"}
    for key in doc:
        if key not in allowed:
            rep.error("registry", f"unknown key '{key}'")
    if doc.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        rep.error("registry", f"schema_version must be {SUPPORTED_SCHEMA_VERSION}")
    if not isinstance(doc.get("name"), str) or not doc.get("name"):
        rep.error("registry", "name must be a non-empty string")
    if not (isinstance(doc.get("updated"), str) and DATE_RE.match(doc["updated"])):
        rep.error("registry", "updated must be a YYYY-MM-DD date")
    plugins = doc.get("plugins")
    if not isinstance(plugins, list):
        rep.error("registry", "plugins must be a list")
        return rep
    seen: dict[str, int] = {}
    for i, entry in enumerate(plugins):
        rep.extend(validate_entry(entry, i, allow_local))
        pid = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(pid, str):
            if pid in seen:
                rep.error(f"plugins[{i}] ({pid})", f"duplicate id (also plugins[{seen[pid]}])")
            seen[pid] = i
    ids = [p.get("id") for p in plugins if isinstance(p, dict) and isinstance(p.get("id"), str)]
    if ids != sorted(ids):
        rep.warn("registry", "plugins are not sorted by id (keeps diffs small)")
    return rep


# --------------------------------------------------------------------------- manifest


def _confined(root: Path, rel: object) -> Path | None:
    """Resolve a manifest path inside root; None when it is absolute or escapes."""
    if not isinstance(rel, str) or not rel or "\\" in rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return None
    if any(part == ".." for part in rel.split("/")):
        return None
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _scan_tree(root: Path, rep: Report) -> None:
    """Refuse symlinks anywhere and enforce the size cap (the .git folder is ignored)."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if full.is_symlink() or _is_reparse_point(full):
                rep.error("tree", f"{rel} is a symbolic link or junction (not allowed)")
        for name in filenames:
            full = Path(dirpath) / name
            if not full.is_symlink():
                total += full.stat().st_size
    if total > MAX_TOTAL_BYTES:
        rep.error("tree", f"plugin is {total / 1048576:.1f} MB; the limit is {MAX_TOTAL_BYTES // 1048576} MB")


def _is_reparse_point(path: Path) -> bool:
    """True for a Windows junction or other reparse point (is_symlink misses junctions)."""
    attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _load_json(path: Path, rep: Report, where: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        rep.error(where, f"cannot read {path.name}: {exc}")
        return None


def _check_theme(root: Path, item: dict, rep: Report, where: str) -> None:
    folder = _confined(root, item.get("path"))
    if folder is None:
        rep.error(where, "path must be relative and stay inside the plugin")
        return
    if not folder.is_dir():
        rep.error(where, f"{item['path']} is not a folder")
        return
    meta_path, css_path = folder / "theme.json", folder / "theme.css"
    if not meta_path.is_file():
        rep.error(where, "theme.json is missing")
    else:
        meta = _load_json(meta_path, rep, where)
        if isinstance(meta, dict):
            if not isinstance(meta.get("displayName"), str) or not meta["displayName"]:
                rep.error(where, "theme.json needs a displayName")
        elif meta is not None:
            rep.error(where, "theme.json must be an object")
    if not css_path.is_file():
        rep.error(where, "theme.css is missing")
    else:
        css = css_path.read_text(encoding="utf-8", errors="replace")
        # Comments may mention @import; only real rules count.
        css = CSS_COMMENT_RE.sub("", css)
        if REMOTE_CSS_RE.search(css):
            rep.error(where, "theme.css loads remote resources (@import or url(//...)); ship assets inside the plugin")
    preview = folder / "preview.png"
    if preview.exists() and preview.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        rep.error(where, "preview.png is not a PNG file")


def _check_persona(root: Path, item: dict, rep: Report, where: str, libraries: set[str]) -> None:
    path = _confined(root, item.get("path"))
    if path is None:
        rep.error(where, "path must be relative and stay inside the plugin")
        return
    if path.suffix.lower() != ".json" or not path.is_file():
        rep.error(where, f"{item['path']} must be an existing .json file")
        return
    data = _load_json(path, rep, where)
    if data is None:
        return
    if not isinstance(data, dict):
        rep.error(where, "persona file must be a JSON object")
        return
    for key in ("name", "system_prompt"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            rep.error(where, f"'{key}' must be a non-empty string")
    for key in data:
        if key not in PERSONA_FIELDS:
            rep.warn(where, f"'{key}' is not a plugin persona field; the app will drop it")
    for key, allowed in (("web_search_mode", {"off", "tool", "auto"}), ("knowledge_mode", {"off", "tool", "auto"})):
        if key in data and data[key] not in allowed:
            rep.error(where, f"{key} must be one of {sorted(allowed)}")
    if "knowledge_top_k" in data and not (isinstance(data["knowledge_top_k"], int) and 1 <= data["knowledge_top_k"] <= 10):
        rep.error(where, "knowledge_top_k must be an integer from 1 to 10")
    refs = data.get("knowledge_plugin_libraries", [])
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        rep.error(where, "knowledge_plugin_libraries must be a list of library names")
    else:
        for ref in refs:
            if ref not in libraries:
                rep.error(where, f"knowledge_plugin_libraries names '{ref}', which this plugin's codex does not contribute")


def _check_codex(root: Path, item: dict, rep: Report, where: str) -> None:
    library = item.get("library")
    if not (isinstance(library, str) and LIBRARY_RE.match(library) and library.strip() == library):
        rep.error(where, "library must be a plain name (no / \\ : * ? \" < > |, not starting with _ or .)")
    folder = _confined(root, item.get("path"))
    if folder is None:
        rep.error(where, "path must be relative and stay inside the plugin")
        return
    if not folder.is_dir():
        rep.error(where, f"{item['path']} is not a folder")
        return
    docs = [p for p in folder.rglob("*.md") if not any(part.startswith(("_", ".")) for part in p.relative_to(folder).parts)]
    if not docs:
        rep.error(where, "the folder contains no indexable .md files")
    for doc in docs:
        try:
            doc.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            rep.error(where, f"{doc.relative_to(root).as_posix()} is not UTF-8")
    others = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() != ".md"]
    if others:
        rep.warn(where, f"{len(others)} non-Markdown file(s) will be ignored by the Codex (e.g. {others[0].name})")


def validate_manifest(root: Path, expect_id: str | None = None, expect_version: str | None = None,
                      expect_kinds: list[str] | None = None) -> tuple[Report, dict | None]:
    """Check a plugin checkout. Returns the report and the parsed manifest (or None)."""
    rep = Report()
    root = Path(root)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        rep.error("manifest", f"{MANIFEST_NAME} not found at the repository root")
        return rep, None
    _scan_tree(root, rep)
    man = _load_json(manifest_path, rep, "manifest")
    if not isinstance(man, dict):
        if man is not None:
            rep.error("manifest", "must be a JSON object")
        return rep, None

    allowed = {"$schema", "manifest_version", "id", "name", "version", "description",
               "author", "license", "app", "contributes"}
    for key in man:
        if key not in allowed:
            rep.error("manifest", f"unknown key '{key}'")
    if man.get("manifest_version") != SUPPORTED_MANIFEST_VERSION:
        rep.error("manifest", f"manifest_version must be {SUPPORTED_MANIFEST_VERSION}")
    if not (isinstance(man.get("id"), str) and ID_RE.match(man["id"])):
        rep.error("manifest", "id must match ^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
    if not (isinstance(man.get("version"), str) and SEMVER_RE.match(man["version"])):
        rep.error("manifest", "version must be semantic (1.2.3)")
    for key, max_len in (("name", 60), ("description", 300), ("license", 60)):
        if not isinstance(man.get(key), str) or not man[key].strip() or len(man[key]) > max_len:
            rep.error("manifest", f"'{key}' must be a non-empty string of at most {max_len} characters")
    author = man.get("author")
    if not (isinstance(author, dict) and isinstance(author.get("name"), str) and author["name"]):
        rep.error("manifest", "author must be an object with a name")
    elif "url" in author and not str(author["url"]).startswith("https://"):
        rep.error("manifest", "author.url must be an https:// URL")
    app = man.get("app")
    if not isinstance(app, dict):
        rep.error("manifest", "app must be an object with min_version and plugin_api")
    else:
        if not (isinstance(app.get("min_version"), str) and APP_VERSION_RE.match(app["min_version"])):
            rep.error("manifest", "app.min_version must look like 3.0.0")
        if "max_version" in app and not (isinstance(app["max_version"], str) and APP_VERSION_RE.match(app["max_version"])):
            rep.error("manifest", "app.max_version must look like 3.9.0")
        if app.get("plugin_api") not in SUPPORTED_PLUGIN_API:
            rep.error("manifest", f"app.plugin_api must be one of {sorted(SUPPORTED_PLUGIN_API)}")

    contributes = man.get("contributes")
    if not (isinstance(contributes, dict) and contributes):
        rep.error("manifest", "contributes must be a non-empty object")
        contributes = {}
    for kind in contributes:
        if kind == "backend":
            rep.error("contributes", "backend code plugins are not supported by plugin_api 1")
        elif kind not in KINDS:
            rep.error("contributes", f"unknown kind '{kind}' (allowed: {list(KINDS)})")

    libraries = {c.get("library") for c in contributes.get("codex", []) if isinstance(c, dict)}
    for kind in KINDS:
        items = contributes.get(kind)
        if items is None:
            continue
        if not isinstance(items, list) or not items:
            rep.error("contributes", f"{kind} must be a non-empty list")
            continue
        seen: set[str] = set()
        for i, item in enumerate(items):
            where = f"contributes.{kind}[{i}]"
            if not isinstance(item, dict):
                rep.error(where, "must be an object")
                continue
            keys = {"library", "path"} if kind == "codex" else {"id", "path"}
            if set(item) != keys:
                rep.error(where, f"must have exactly the keys {sorted(keys)}")
                continue
            key = item["library"] if kind == "codex" else item["id"]
            if kind != "codex" and not (isinstance(key, str) and SUB_ID_RE.match(key)):
                rep.error(where, "id must be lowercase letters, digits and hyphens")
            if key in seen:
                rep.error(where, f"duplicate {'library' if kind == 'codex' else 'id'} '{key}'")
            seen.add(key)
            if kind == "themes":
                _check_theme(root, item, rep, where)
            elif kind == "personas":
                _check_persona(root, item, rep, where, libraries)
            else:
                _check_codex(root, item, rep, where)

    if expect_id is not None and man.get("id") != expect_id:
        rep.error("manifest", f"id is '{man.get('id')}' but the registry entry says '{expect_id}'")
    if expect_version is not None and man.get("version") != expect_version:
        rep.error("manifest", f"version is '{man.get('version')}' but the registry entry says '{expect_version}'")
    if expect_kinds is not None and sorted(expect_kinds) != sorted(k for k in contributes if k in KINDS):
        rep.error("manifest", f"contributes {sorted(contributes)} but the registry entry lists kinds {sorted(expect_kinds)}")
    for name in ("README.md", "LICENSE"):
        if not (root / name).is_file():
            rep.warn("tree", f"{name} is missing")
    return rep, man


# --------------------------------------------------------------------------- git


def _git(args: list[str], cwd: Path | None = None) -> str:
    """Run git non-interactively; raise RuntimeError with git's message on failure."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="echo")
    try:
        done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=GIT_TIMEOUT_S)
    except FileNotFoundError as exc:
        raise RuntimeError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {args[0]} timed out after {GIT_TIMEOUT_S} s") from exc
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def resolve_tag(repo: str, tag: str) -> str:
    """Return the commit a tag points at (annotated tags are peeled)."""
    out = _git(["ls-remote", "--tags", repo, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"])
    refs = dict(reversed(line.split("\t", 1)) for line in out.splitlines() if "\t" in line)
    commit = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
    if not commit:
        raise RuntimeError(f"tag '{tag}' not found in {repo}")
    return commit.strip()


def clone_at_tag(repo: str, tag: str, dest: Path) -> str:
    """Shallow-clone a tag into dest; return HEAD."""
    _git(["clone", "--quiet", "--depth", "1", "--branch", tag, "--no-tags", repo, str(dest)])
    return _git(["rev-parse", "HEAD"], cwd=dest).strip()


def _rmtree(path: Path) -> None:
    """Remove a checkout; git marks pack files read-only, which Windows refuses to delete."""
    def make_writable(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:
        shutil.rmtree(path, onerror=make_writable)


def check_entry_remote(entry: dict) -> Report:
    """Tag → commit, clone, and manifest match for one entry."""
    rep = Report()
    where = f"{entry.get('id')}"
    try:
        tag_commit = resolve_tag(entry["repo"], entry["tag"])
    except RuntimeError as exc:
        rep.error(where, str(exc))
        return rep
    if tag_commit != entry["commit"]:
        rep.error(where, f"tag {entry['tag']} points at {tag_commit}, but the entry pins {entry['commit']}")
        return rep
    tmp = Path(tempfile.mkdtemp(prefix="stc-check-"))
    try:
        head = clone_at_tag(entry["repo"], entry["tag"], tmp / "repo")
        if head != entry["commit"]:
            rep.error(where, f"cloned HEAD {head} differs from the pinned commit")
            return rep
        sub, _ = validate_manifest(tmp / "repo", entry["id"], entry["version"], entry["kinds"])
        for e in sub.errors:
            rep.error(where, e)
        for w in sub.warnings:
            rep.warn(where, w)
    except RuntimeError as exc:
        rep.error(where, str(exc))
    finally:
        _rmtree(tmp)
    return rep


# --------------------------------------------------------------------------- commands


def _read_registry(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def cmd_validate(args: argparse.Namespace) -> int:
    rep = Report()
    try:
        doc = _read_registry(Path(args.registry))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR registry: cannot read {args.registry}: {exc}")
        return 1
    rep.extend(validate_registry_doc(doc, args.allow_local))
    if args.remote and isinstance(doc, dict) and isinstance(doc.get("plugins"), list):
        for i, entry in enumerate(doc["plugins"]):
            if not isinstance(entry, dict) or (args.only and entry.get("id") not in args.only):
                continue
            if validate_entry(entry, i, args.allow_local).errors:
                continue  # already reported; remote checks need a well-formed entry
            print(f"checking {entry['id']} {entry['version']} ({entry['repo']}) ...")
            rep.extend(check_entry_remote(entry))
    rep.print()
    return 1 if rep.errors else 0


def cmd_manifest(args: argparse.Namespace) -> int:
    rep, _ = validate_manifest(Path(args.dir), args.expect_id, args.expect_version)
    rep.print()
    return 1 if rep.errors else 0


def cmd_pin(args: argparse.Namespace) -> int:
    problem = _check_repo_url(args.repo, args.allow_local)
    if problem:
        print(f"ERROR {problem}")
        return 1
    path = Path(args.registry)
    doc = _read_registry(path)
    tmp = Path(tempfile.mkdtemp(prefix="stc-pin-"))
    try:
        commit = resolve_tag(args.repo, args.tag)
        head = clone_at_tag(args.repo, args.tag, tmp / "repo")
        if head != commit:
            print(f"ERROR cloned HEAD {head} differs from tag commit {commit}")
            return 1
        rep, man = validate_manifest(tmp / "repo")
        rep.print()
        if rep.errors or man is None:
            print("Not pinned: fix the plugin first.")
            return 1
    except RuntimeError as exc:
        print(f"ERROR {exc}")
        return 1
    finally:
        _rmtree(tmp)

    author = man["author"]["name"]
    fresh = {
        "id": man["id"], "name": man["name"], "description": man["description"], "author": author,
        "repo": args.repo, "version": man["version"], "tag": args.tag, "commit": commit,
        "kinds": [k for k in KINDS if k in man["contributes"]],
        "min_app_version": man["app"]["min_version"], "plugin_api": man["app"]["plugin_api"],
        "license": man["license"],
    }
    if "max_version" in man["app"]:
        fresh["max_app_version"] = man["app"]["max_version"]
    plugins = doc.setdefault("plugins", [])
    for i, entry in enumerate(plugins):
        if entry.get("id") == man["id"]:
            if entry.get("repo") != args.repo:
                print(f"ERROR id '{man['id']}' is registered for {entry.get('repo')}; ids are never reused")
                return 1
            kept = {k: v for k, v in entry.items() if k in ("homepage", "tags", "deprecated")}
            plugins[i] = {**fresh, **kept}
            action = f"updated {entry.get('version')} -> {man['version']}"
            break
    else:
        plugins.append(fresh)
        action = f"added {man['version']}"
    plugins.sort(key=lambda e: e.get("id", ""))
    doc["updated"] = _dt.date.today().isoformat()
    final = validate_registry_doc(doc, args.allow_local)
    if final.errors:
        final.print()
        return 1
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"{man['id']}: {action} at {commit}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STC Index tool (validate registry / plugin, pin versions)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="check registry.json")
    p.add_argument("registry")
    p.add_argument("--remote", action="store_true", help="also check every repository (needs git + network)")
    p.add_argument("--only", nargs="*", help="limit --remote to these ids")
    p.add_argument("--allow-local", action="store_true", help="accept file:// repositories (tests only)")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("manifest", help="check a plugin checkout")
    p.add_argument("dir")
    p.add_argument("--expect-id")
    p.add_argument("--expect-version")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("pin", help="add or update a registry entry from a tagged plugin release")
    p.add_argument("registry")
    p.add_argument("--repo", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--allow-local", action="store_true", help="accept file:// repositories (tests only)")
    p.set_defaults(func=cmd_pin)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
