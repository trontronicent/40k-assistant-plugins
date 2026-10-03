# Submitting a plugin

## 1. Build the plugin

1. Create a repository from the
   [plugin template](https://github.com/trontronicent/40k-assistant-plugin-template).
2. Edit `strategicum-plugin.json`: pick a unique `id` (lowercase letters, digits,
   hyphens, 3–40 characters; it can never be reused), name, description, author.
3. Add your themes, personas and Codex folders and list each one under
   `contributes`. Remove the kinds you do not use.
4. Check it:

   ```
   python tools/registry_tool.py manifest path/to/your/plugin
   ```

   (Download `tools/registry_tool.py` from this repository; it needs only Python.)

## 2. Release a version

```
git tag v1.0.0
git push origin v1.0.0
```

The version in `strategicum-plugin.json` must equal the tag without the `v`.

## 3. Add it to the index

Fork this repository, then:

```
python tools/registry_tool.py pin registry.json --repo https://github.com/<you>/<plugin>.git --tag v1.0.0
python tools/registry_tool.py validate registry.json --remote
```

`pin` resolves the tag to its commit, clones it, validates the manifest and
writes the entry (id, name, description, version, tag, commit, kinds, app
versions, license). You may add `homepage` and `tags` by hand. Open a pull
request. There is no automatic CI: a maintainer runs
`validate --remote` on your branch and reviews the plugin diff before merging.

Optional, recommended in your fork: `git config core.hooksPath tools/hooks`
runs the offline check on every commit of `registry.json` and the remote
check before every push.

## Updating a plugin

Bump `version`, tag, push the tag, run `pin` again (it keeps `homepage` and
`tags`) and open a pull request. Users see *Update available* once it is merged.

## Rules

- **Never move or delete a released tag.** The index pins the commit; a moved
  tag makes the check fail and blocks every later update until fixed.
- Persona files may only use the fields listed in the template's README; the
  app drops anything else.
- Theme CSS must not load anything from the internet (`@import`, `url(//…)`).
- No binaries except `preview.png`; the whole plugin stays under 50 MB.
- To withdraw a version, a maintainer sets `"yanked": "<reason>"` on the entry.
