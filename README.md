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

## What a plugin can contribute (plugin API 1)

| Kind | Contents | Becomes in the app |
|---|---|---|
| `themes` | `theme.json` + `theme.css` (+ `preview.png`) | a selectable theme |
| `personas` | one JSON file per persona | a persona (marked *STC*) |
| `codex` | a folder of Markdown files | a read-only Codex library |

Plugin API 1 runs **no plugin code**. Backend code plugins are planned for a
later API version with an explicit trust step.

## Files

| File | Purpose |
|---|---|
| `registry.json` | the index the app reads |
| `schema/registry.schema.json` | JSON Schema for `registry.json` |
| `schema/plugin-manifest.schema.json` | JSON Schema for a plugin's `strategicum-plugin.json` |
| `tools/registry_tool.py` | validate the index, validate a plugin, pin a version (Python 3.9+, no dependencies) |
| `tools/hooks/` | git hooks: offline check on commit, remote check on push (`git config core.hooksPath tools/hooks`) |

There is no CI; checks run locally through the tool and the hooks.

## Writing a plugin

Start from the [plugin template](https://github.com/trontronicent/40k-assistant-plugin-template)
(*Use this template*), then follow [CONTRIBUTING.md](CONTRIBUTING.md).

## Using another registry

The app accepts several registry URLs (STC Archive → *Sources*). Plugins from a
registry other than this one are shown as *unreviewed*.
