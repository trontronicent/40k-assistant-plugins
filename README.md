# Strategicum STC Index

The list of plugins (**STCs**, Standard Template Constructs) for the
[40k Assistant / Strategicum](https://github.com/trontronicent/40k-assistant).

The app's **STC Archive** page downloads [`registry.json`](registry.json) from

```
https://raw.githubusercontent.com/trontronicent/40k-assistant-plugins/main/registry.json
```

and clones a plugin from the repository listed in its entry, at the **pinned
commit**. If the cloned commit differs from the pinned one, the app refuses to
install it.

## What a plugin can contribute

| Kind | Contents | Becomes in the app |
|---|---|---|
| `themes` | `theme.json` + `theme.css` (+ `preview.png`) | a selectable theme |
| `personas` | one JSON file per persona | a persona (marked *STC*) |
| `codex` | a folder of Markdown files | a read-only Codex library |
| `backend` (plugin API 2) | a Python package (`module`, `entry`) | code running inside the app; its data appears in the **Plugins** menu |

Plugin API 1 runs **no plugin code**. A `backend` plugin (API 2) runs inside the
app with the app's full rights: the app installs it only after the user confirms
they trust it, and only from the computer the app runs on. Its `entry(ctx)`
returns an object with `start()`, `stop()`, `view()` (a declarative view: tables,
key/value lists, notices - never HTML) and optionally `action(id, params)`.
The app never installs Python packages for a plugin; use the standard library.

The current app installs `backend` plugins; adapters for `themes`, `personas`
and `codex` follow (the app shows such plugins as not installable yet).

Every plugin can also bring **help and credits** (app 3.4.0+): `help` names a
Markdown file in the plugin (at most 100 KB) and `credits` lists who made or
contributed what (`[{name, for?, url?}]`, `https://` links only). The app shows
both in its manual, in a section of the *Plugin Help* chapter, and the plugin
list shows the manifest's `author` as a tag.

## Files

| File | Purpose |
|---|---|
| `registry.json` | the index the app reads |
| `schema/registry.schema.json` | JSON Schema for `registry.json` |
| `schema/plugin-manifest.schema.json` | JSON Schema for a plugin's `strategicum-plugin.json` |
| `tools/registry_tool.py` | validate the index, validate a plugin, pin a version (Python 3.9+, no dependencies) |
| `tools/hooks/` | git hooks: offline check on commit, remote check on push (`git config core.hooksPath tools/hooks`) |
| `tests/` | the tool's own tests - `python -m unittest discover -s tests` (standard library only, no network) |
| `ruff.toml` | the code shape the tooling keeps, the same limits as the app and the plugins |

There is no CI; checks run locally through the tool, its tests and the hooks. The commit hook runs the
tests when the tool or a test changes, and validates `registry.json` when it is staged.

## Writing a plugin

Start from the [plugin template](https://github.com/trontronicent/40k-assistant-plugin-template)
(*Use this template*), then follow [CONTRIBUTING.md](CONTRIBUTING.md).

## Using another registry

The app accepts several registry URLs (STC Archive → *Sources*). Plugins from a
registry other than this one are shown as *unreviewed*.
