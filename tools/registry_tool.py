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
SUPPORTED_PLUGIN_API = {1, 2}
# List-style contributions; "backend" (plugin API 2) is a single object.
KINDS = ("themes", "personas", "codex")
ALL_KINDS = KINDS + ("backend",)
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# `write-codex` (app 3.14.0) lets a plugin keep documents in one Codex library through ctx.codex.
PERMISSIONS = {"read-game-files", "read-game-memory", "network", "write-codex"}
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
ENTRY_OPTIONAL = ("homepage", "tags", "yanked", "deprecated", "max_app_version", "permissions",
                  "categories", "manifest_url")

# Categories and the per-tag manifest URL (app 3.13.0). The same constants live in the app's
# src/plugins/manifest.py (MAX_CATEGORIES, CATEGORY_RE) and src/plugins/registry.py (MANIFEST_URL_RE):
# this check exists to refuse at review time exactly what the app refuses at install time.
MAX_CATEGORIES = 5
CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
MANIFEST_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/{}-]+$")
MAX_VOCABULARY = 40
MAX_CATEGORY_LABEL = 40
MAX_CATEGORY_DESCRIPTION = 200

PERSONA_MODES = {"off", "tool", "auto"}      # web_search_mode and knowledge_mode
MAX_KNOWLEDGE_TOP_K = 10

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


# An entry's plain text fields and their limits; `yanked` and `deprecated` are optional notes shown to the user.
ENTRY_TEXTS = (("name", 60), ("description", 300), ("author", 80), ("license", 60), ("tag", 100),
               ("yanked", 300), ("deprecated", 300))
MAX_ENTRY_TAGS = 10


def _entry_text_fields(entry: dict, where: str, rep: Report) -> None:
    """Every text field of an entry that is present must be non-empty and within its limit."""
    for key, max_len in ENTRY_TEXTS:
        value = entry.get(key)
        if key in entry and (not isinstance(value, str) or not value.strip() or len(value) > max_len):
            rep.error(where, f"'{key}' must be a non-empty string of at most {max_len} characters")


def _entry_identity(entry: dict, where: str, allow_local: bool, rep: Report) -> None:
    """The id, the clone URL, the version and the tag that must match it."""
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


def _entry_commit(entry: dict, where: str, rep: Report) -> None:
    """The pinned commit: a full lowercase hash, and not the all-zero placeholder of a hand-written entry."""
    if "commit" not in entry:
        return
    commit = entry["commit"]
    if not (isinstance(commit, str) and COMMIT_RE.match(commit)):
        rep.error(where, "commit must be the full 40-character lowercase hash")
    elif set(commit) == {"0"}:
        rep.error(where, "commit is still the placeholder; run: registry_tool.py pin ...")


def _entry_app_fields(entry: dict, where: str, rep: Report) -> None:
    """What the entry promises about the app: version range, plugin API, kinds, permissions."""
    for key in ("min_app_version", "max_app_version"):
        if key in entry and not (isinstance(entry[key], str) and SEMVER_RE.match(entry[key])):
            rep.error(where, f"{key} must be semantic (3.0.0)")
    if "kinds" in entry:
        kinds = entry["kinds"]
        if not (isinstance(kinds, list) and kinds and all(k in ALL_KINDS for k in kinds)
                and len(set(kinds)) == len(kinds)):
            rep.error(where, f"kinds must be a non-empty list of distinct values from {list(ALL_KINDS)}")
    if "plugin_api" in entry and entry["plugin_api"] not in SUPPORTED_PLUGIN_API:
        rep.error(where, f"plugin_api must be one of {sorted(SUPPORTED_PLUGIN_API)}")
    if "permissions" in entry and not (isinstance(entry["permissions"], list)
                                       and all(p in PERMISSIONS for p in entry["permissions"])):
        rep.error(where, f"permissions must be a list from {sorted(PERMISSIONS)}")


def _entry_listing_fields(entry: dict, where: str, rep: Report) -> None:
    """What the index page shows beside the name: the homepage link and the search tags."""
    if "homepage" in entry and not (isinstance(entry["homepage"], str) and entry["homepage"].startswith("https://")):
        rep.error(where, "homepage must be an https:// URL")
    if "tags" in entry:
        tags = entry["tags"]
        if not (isinstance(tags, list) and len(tags) <= MAX_ENTRY_TAGS and len(set(map(str, tags))) == len(tags)
                and all(isinstance(t, str) and TAG_NAME_RE.match(t) for t in tags)):
            rep.error(where, f"tags must be at most {MAX_ENTRY_TAGS} distinct lowercase words (a-z, 0-9, -)")


def _categories_problem(raw: object) -> str | None:
    """Why a `categories` value is unusable, or None. Shared by the entry and the manifest check."""
    if not (isinstance(raw, list) and len(raw) <= MAX_CATEGORIES):
        return f"categories must be a list of at most {MAX_CATEGORIES} entries"
    bad = [c for c in raw if not (isinstance(c, str) and CATEGORY_RE.match(c))]
    if bad:
        return f"categories must match {CATEGORY_RE.pattern} ({bad[0]!r} does not)"
    return None


def _entry_classification(entry: dict, where: str, rep: Report) -> None:
    """What the app files the plugin under, and where it reads the manifest of each tag (app 3.13.0)."""
    if "categories" in entry:
        problem = _categories_problem(entry["categories"])
        if problem:
            rep.error(where, problem)
    if "manifest_url" in entry:
        url = entry["manifest_url"]
        if not (isinstance(url, str) and MANIFEST_URL_RE.match(url) and "{ref}" in url and ".." not in url):
            rep.error(where, "manifest_url must be an https URL containing the {ref} placeholder")


def vocabulary_problems(raw: object) -> list[str]:
    """Why a registry document's `categories` vocabulary is unusable; [] when it is fine.

    The vocabulary only labels and orders the app's filter chips, but it is the one place a new category
    ships without an app release - so a typo here silently renames a chip for every user.
    """
    if not isinstance(raw, list):
        return ["categories must be a list of {id, label} objects"]
    if len(raw) > MAX_VOCABULARY:
        return [f"categories must hold at most {MAX_VOCABULARY} entries"]
    problems, seen = [], set()
    for i, item in enumerate(raw):
        at = f"categories[{i}]"
        if not isinstance(item, dict):
            problems.append(f"{at} must be an object")
            continue
        unknown = [k for k in item if k not in ("id", "label", "description", "order")]
        problems += [f"{at}: unknown key '{k}'" for k in unknown]
        slug, label = item.get("id"), item.get("label")
        if not (isinstance(slug, str) and CATEGORY_RE.match(slug)):
            problems.append(f"{at}.id must match {CATEGORY_RE.pattern}")
        elif slug in seen:
            problems.append(f"{at}.id '{slug}' is listed twice")
        else:
            seen.add(slug)
        if not (isinstance(label, str) and label.strip() and len(label) <= MAX_CATEGORY_LABEL):
            problems.append(f"{at}.label must be a non-empty string of at most {MAX_CATEGORY_LABEL} characters")
        description = item.get("description")
        if description is not None and not (isinstance(description, str)
                                           and len(description) <= MAX_CATEGORY_DESCRIPTION):
            problems.append(f"{at}.description must be a string of at most {MAX_CATEGORY_DESCRIPTION} characters")
        order = item.get("order")
        if order is not None and not (isinstance(order, int) and not isinstance(order, bool)):
            problems.append(f"{at}.order must be a whole number")
    return problems


def validate_entry(entry: object, index: int, allow_local: bool = False) -> Report:
    """Check one registry entry's shape and values (no network).

    A thin assembler over the section checks above: keys, texts, identity, commit, app promises, listing."""
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
    _entry_text_fields(entry, where, rep)
    _entry_identity(entry, where, allow_local, rep)
    _entry_commit(entry, where, rep)
    _entry_app_fields(entry, where, rep)
    _entry_listing_fields(entry, where, rep)
    _entry_classification(entry, where, rep)
    return rep


DOC_KEYS = {"$schema", "schema_version", "name", "updated", "plugins", "categories"}


def _doc_header(doc: dict, rep: Report) -> None:
    """Everything in the registry document except the plugin list: keys, version, name, date, vocabulary."""
    for key in doc:
        if key not in DOC_KEYS:
            rep.error("registry", f"unknown key '{key}'")
    if doc.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        rep.error("registry", f"schema_version must be {SUPPORTED_SCHEMA_VERSION}")
    if not isinstance(doc.get("name"), str) or not doc.get("name"):
        rep.error("registry", "name must be a non-empty string")
    if not (isinstance(doc.get("updated"), str) and DATE_RE.match(doc["updated"])):
        rep.error("registry", "updated must be a YYYY-MM-DD date")
    if "categories" in doc:
        for problem in vocabulary_problems(doc["categories"]):
            rep.error("registry", problem)


def validate_registry_doc(doc: object, allow_local: bool = False) -> Report:
    """Check the whole registry document (no network)."""
    rep = Report()
    if not isinstance(doc, dict):
        rep.error("registry", "must be a JSON object")
        return rep
    _doc_header(doc, rep)
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


def _persona_file(root: Path, item: dict, rep: Report, where: str) -> dict | None:
    """The parsed persona document, or None with the reason reported (path, suffix, JSON, type)."""
    path = _confined(root, item.get("path"))
    if path is None:
        rep.error(where, "path must be relative and stay inside the plugin")
        return None
    if path.suffix.lower() != ".json" or not path.is_file():
        rep.error(where, f"{item['path']} must be an existing .json file")
        return None
    data = _load_json(path, rep, where)
    if data is None:
        return None
    if not isinstance(data, dict):
        rep.error(where, "persona file must be a JSON object")
        return None
    return data


def _persona_knowledge(data: dict, rep: Report, where: str, libraries: set[str]) -> None:
    """The Codex and web-search settings, and the libraries the persona claims.

    A library name the manifest does not contribute leaves the persona with no sources at all, which looks like a
    broken Codex rather than a typo."""
    for key, allowed in (("web_search_mode", PERSONA_MODES), ("knowledge_mode", PERSONA_MODES)):
        if key in data and data[key] not in allowed:
            rep.error(where, f"{key} must be one of {sorted(allowed)}")
    if "knowledge_top_k" in data and not (isinstance(data["knowledge_top_k"], int)
                                          and 1 <= data["knowledge_top_k"] <= MAX_KNOWLEDGE_TOP_K):
        rep.error(where, f"knowledge_top_k must be an integer from 1 to {MAX_KNOWLEDGE_TOP_K}")
    refs = data.get("knowledge_plugin_libraries", [])
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        rep.error(where, "knowledge_plugin_libraries must be a list of library names")
        return
    for ref in refs:
        if ref not in libraries:
            rep.error(where, f"knowledge_plugin_libraries names '{ref}', which this plugin's codex does not contribute")


def _check_persona(root: Path, item: dict, rep: Report, where: str, libraries: set[str]) -> None:
    """One persona contribution: the file, its required fields, the fields the app would drop, its settings."""
    data = _persona_file(root, item, rep, where)
    if data is None:
        return
    for key in ("name", "system_prompt"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            rep.error(where, f"'{key}' must be a non-empty string")
    for key in data:
        if key not in PERSONA_FIELDS:
            rep.warn(where, f"'{key}' is not a plugin persona field; the app will drop it")
    _persona_knowledge(data, rep, where, libraries)


def _check_codex(root: Path, item: dict, rep: Report, where: str) -> None:
    library = item.get("library")
    if not (isinstance(library, str) and LIBRARY_RE.match(library) and library.strip() == library):
        rep.error(where, "library must be a plain name (no / \\ : * ? \" < > |, not starting with _ or .)")
    if "path" not in item:
        # app 3.14.0: no shipped folder - the plugin writes its documents at runtime through
        # ctx.codex, and the library named above is where they go.
        return
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


def _check_backend(root: Path, backend: object, plugin_api: object, rep: Report) -> None:
    """Plugin API 2: a Python package the app imports and runs (needs a trust confirmation there)."""
    where = "contributes.backend"
    if plugin_api != 2:
        rep.error(where, "needs app.plugin_api 2")
    if not isinstance(backend, dict):
        rep.error(where, "must be an object")
        return
    for key in backend:
        if key not in ("module", "entry", "permissions", "python_requires"):
            rep.error(where, f"unknown key '{key}'")
    module, entry = backend.get("module"), backend.get("entry")
    if not (isinstance(module, str) and IDENTIFIER_RE.match(module)):
        rep.error(where, "module must be a Python package name")
    elif not (root / module / "__init__.py").is_file():
        rep.error(where, f"{module}/__init__.py not found")
    if not (isinstance(entry, str) and IDENTIFIER_RE.match(entry)):
        rep.error(where, "entry must be a function name")
    perms = backend.get("permissions", [])
    if not (isinstance(perms, list) and all(p in PERMISSIONS for p in perms)):
        rep.error(where, f"permissions must be a list from {sorted(PERMISSIONS)}")
    if backend.get("python_requires"):
        rep.warn(where, "python_requires: the app never installs packages; list them in the README")


MAX_HELP_BYTES = 100 * 1024
MAX_CREDITS = 30
CREDIT_KEYS = {"name", "for", "url"}


def _check_help(root: Path, man: dict, rep: "Report") -> None:
    """Optional `help`: a .md file inside the plugin, UTF-8, <= 100 KB, shown in the app's manual under
    Plugin Help. Same rules as the app."""
    rel = man.get("help")
    if rel is None:
        return
    path = _confined(root, rel)
    if path is None or not str(rel).lower().endswith(".md"):
        rep.error("manifest", "help must be the relative path of a .md file inside the plugin")
    elif not path.is_file():
        rep.error("manifest", f"help file {rel} not found")
    elif path.stat().st_size > MAX_HELP_BYTES:
        rep.error("manifest", f"help file {rel} is larger than {MAX_HELP_BYTES // 1024} KB")
    else:
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            rep.error("manifest", f"help file {rel} is not UTF-8")


def _check_credit(item: object, where: str, rep: "Report") -> None:
    """One credit: {name, for?, url?} with https links only."""
    if not isinstance(item, dict):
        rep.error("manifest", f"{where} must be an object")
        return
    for key in item:
        if key not in CREDIT_KEYS:
            rep.error("manifest", f"{where}: unknown key '{key}'")
    name, what, url = item.get("name"), item.get("for"), item.get("url")
    if not (isinstance(name, str) and name.strip() and len(name) <= 80):
        rep.error("manifest", f"{where}.name must be a non-empty string of at most 80 characters")
    if what is not None and not (isinstance(what, str) and len(what) <= 200):
        rep.error("manifest", f"{where}.for must be a string of at most 200 characters")
    if url is not None and not (isinstance(url, str) and url.startswith("https://") and len(url) <= 200):
        rep.error("manifest", f"{where}.url must be an https:// URL of at most 200 characters")


def _check_help_and_credits(root: Path, man: dict, rep: "Report") -> None:
    """Optional `help` (`_check_help`) and `credits` ([{name, for?, url?}], <= 30, `_check_credit`)."""
    _check_help(root, man, rep)
    credits = man.get("credits")
    if credits is None:
        return
    if not isinstance(credits, list) or len(credits) > MAX_CREDITS:
        rep.error("manifest", f"credits must be a list of at most {MAX_CREDITS} entries")
        return
    for i, item in enumerate(credits):
        _check_credit(item, f"credits[{i}]", rep)


MANIFEST_KEYS = {"$schema", "manifest_version", "id", "name", "version", "description",
                 "author", "license", "app", "contributes", "help", "credits", "data_version",
                 "categories"}
MANIFEST_TEXTS = (("name", 60), ("description", 300), ("license", 60))
MAX_DATA_VERSION = 1_000_000
# The one check per contribution kind, so adding a kind is a row here and not a branch in the loop.
KIND_CHECKS = {"themes": _check_theme, "codex": _check_codex}


def _manifest_identity(man: dict, rep: Report) -> None:
    """The keys the app knows, the format version, the id, the version and the plain text fields."""
    for key in man:
        if key not in MANIFEST_KEYS:
            rep.error("manifest", f"unknown key '{key}'")
    if man.get("manifest_version") != SUPPORTED_MANIFEST_VERSION:
        rep.error("manifest", f"manifest_version must be {SUPPORTED_MANIFEST_VERSION}")
    if not (isinstance(man.get("id"), str) and ID_RE.match(man["id"])):
        rep.error("manifest", "id must match ^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
    if not (isinstance(man.get("version"), str) and SEMVER_RE.match(man["version"])):
        rep.error("manifest", "version must be semantic (1.2.3)")
    if "categories" in man:
        problem = _categories_problem(man["categories"])
        if problem:
            rep.error("manifest", problem)
    for key, max_len in MANIFEST_TEXTS:
        if not isinstance(man.get(key), str) or not man[key].strip() or len(man[key]) > max_len:
            rep.error("manifest", f"'{key}' must be a non-empty string of at most {max_len} characters")
    author = man.get("author")
    if not (isinstance(author, dict) and isinstance(author.get("name"), str) and author["name"]):
        rep.error("manifest", "author must be an object with a name")
    elif "url" in author and not str(author["url"]).startswith("https://"):
        rep.error("manifest", "author.url must be an https:// URL")


def _manifest_app(man: dict, rep: Report) -> None:
    """The `app` block: the version range this plugin supports and the plugin API it needs."""
    app = man.get("app")
    if not isinstance(app, dict):
        rep.error("manifest", "app must be an object with min_version and plugin_api")
        return
    if not (isinstance(app.get("min_version"), str) and APP_VERSION_RE.match(app["min_version"])):
        rep.error("manifest", "app.min_version must look like 3.0.0")
    if "max_version" in app and not (isinstance(app["max_version"], str) and APP_VERSION_RE.match(app["max_version"])):
        rep.error("manifest", "app.max_version must look like 3.9.0")
    if app.get("plugin_api") not in SUPPORTED_PLUGIN_API:
        rep.error("manifest", f"app.plugin_api must be one of {sorted(SUPPORTED_PLUGIN_API)}")


def _manifest_data_version(man: dict, rep: Report) -> None:
    """Optional `data_version`: a whole number the app compares when a plugin's stored data format changed."""
    data_version = man.get("data_version")
    if data_version is not None and not (isinstance(data_version, int) and not isinstance(data_version, bool)
                                         and 1 <= data_version <= MAX_DATA_VERSION):
        rep.error("manifest", f"data_version must be a whole number from 1 to {MAX_DATA_VERSION}")


def _contribution_key(kind: str, item: dict, seen: set, rep: Report, where: str) -> bool:
    """Check one item's keys and its id/library, and record it. False when the item must not be checked further."""
    keys = {"library", "path"} if kind == "codex" else {"id", "path"}
    if set(item) != keys:
        rep.error(where, f"must have exactly the keys {sorted(keys)}")
        return False
    key = item["library"] if kind == "codex" else item["id"]
    if kind != "codex" and not (isinstance(key, str) and SUB_ID_RE.match(key)):
        rep.error(where, "id must be lowercase letters, digits and hyphens")
    if key in seen:
        rep.error(where, f"duplicate {'library' if kind == 'codex' else 'id'} '{key}'")
    seen.add(key)
    return True


def _check_kind(root: Path, kind: str, items: object, libraries: set, rep: Report) -> None:
    """Every contribution of one list kind: a non-empty list of items with distinct ids and checked files."""
    if not isinstance(items, list) or not items:
        rep.error("contributes", f"{kind} must be a non-empty list")
        return
    seen: set = set()
    for i, item in enumerate(items):
        where = f"contributes.{kind}[{i}]"
        if not isinstance(item, dict):
            rep.error(where, "must be an object")
            continue
        if not _contribution_key(kind, item, seen, rep, where):
            continue
        if kind == "personas":          # the only check that needs the plugin's own libraries
            _check_persona(root, item, rep, where, libraries)
        else:
            KIND_CHECKS[kind](root, item, rep, where)


def _manifest_contributions(root: Path, man: dict, rep: Report) -> dict:
    """Every contribution of the manifest; returns the `contributes` object ({} when it is unusable)."""
    contributes = man.get("contributes")
    if not (isinstance(contributes, dict) and contributes):
        rep.error("manifest", "contributes must be a non-empty object")
        return {}
    for kind in contributes:
        if kind not in ALL_KINDS:
            rep.error("contributes", f"unknown kind '{kind}' (allowed: {list(ALL_KINDS)})")
    app = man.get("app")
    if "backend" in contributes:
        _check_backend(root, contributes["backend"], app.get("plugin_api") if isinstance(app, dict) else None, rep)
    libraries = {c.get("library") for c in contributes.get("codex", []) if isinstance(c, dict)}
    for kind in KINDS:
        if contributes.get(kind) is not None:
            _check_kind(root, kind, contributes[kind], libraries, rep)
    return contributes


def _manifest_expectations(man: dict, contributes: dict, rep: Report, expect: "tuple") -> None:
    """What the registry entry says this manifest must be: (id, version, kinds); None means "do not check"."""
    expect_id, expect_version, expect_kinds = expect
    if expect_id is not None and man.get("id") != expect_id:
        rep.error("manifest", f"id is '{man.get('id')}' but the registry entry says '{expect_id}'")
    if expect_version is not None and man.get("version") != expect_version:
        rep.error("manifest", f"version is '{man.get('version')}' but the registry entry says '{expect_version}'")
    if expect_kinds is not None and sorted(expect_kinds) != sorted(k for k in contributes if k in ALL_KINDS):
        rep.error("manifest", f"contributes {sorted(contributes)} but the registry entry lists kinds {sorted(expect_kinds)}")


def validate_manifest(root: Path, expect_id: str | None = None, expect_version: str | None = None,
                      expect_kinds: list[str] | None = None) -> tuple[Report, dict | None]:
    """Check a plugin checkout. Returns the report and the parsed manifest (or None).

    A thin assembler over the section checks above: the tree, the manifest's own fields, the app block, help and
    credits, data_version, every contribution, and what the registry entry expects."""
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

    _manifest_identity(man, rep)
    _manifest_app(man, rep)
    _check_help_and_credits(root, man, rep)
    _manifest_data_version(man, rep)
    contributes = _manifest_contributions(root, man, rep)
    _manifest_expectations(man, contributes, rep, (expect_id, expect_version, expect_kinds))
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


def entry_from_manifest(man: dict, repo: str, tag: str, commit: str) -> dict:
    """The registry entry a validated manifest describes (pure, so `pin` stays a thin command).

    Note that `version`, `tag` and `commit` are the *frozen fallback* since app 3.13.0: an app from
    3.13.0 on reads the plugin's own repository instead, and only an older app installs this pin.
    """
    fresh = {
        "id": man["id"], "name": man["name"], "description": man["description"],
        "author": man["author"]["name"], "repo": repo, "version": man["version"], "tag": tag,
        "commit": commit, "kinds": [k for k in ALL_KINDS if k in man["contributes"]],
        "min_app_version": man["app"]["min_version"], "plugin_api": man["app"]["plugin_api"],
        "license": man["license"],
    }
    if "max_version" in man["app"]:
        fresh["max_app_version"] = man["app"]["max_version"]
    backend = man["contributes"].get("backend")
    if isinstance(backend, dict) and backend.get("permissions"):
        fresh["permissions"] = backend["permissions"]
    if man.get("categories"):
        fresh["categories"] = list(man["categories"])
    return fresh


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

    fresh = entry_from_manifest(man, args.repo, args.tag, commit)
    plugins = doc.setdefault("plugins", [])
    for i, entry in enumerate(plugins):
        if entry.get("id") == man["id"]:
            if entry.get("repo") != args.repo:
                print(f"ERROR id '{man['id']}' is registered for {entry.get('repo')}; ids are never reused")
                return 1
            kept = {k: v for k, v in entry.items()
                    if k in ("homepage", "tags", "deprecated", "manifest_url")}
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
