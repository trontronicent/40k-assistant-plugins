## Plugin submission / update

- Plugin id: `...`
- Repository: https://github.com/.../....git
- Version / tag: `vX.Y.Z`

### Checklist

- [ ] The entry was written with `python tools/registry_tool.py pin registry.json --repo <url> --tag <tag>` (not by hand).
- [ ] `python tools/registry_tool.py validate registry.json --remote` passes locally.
- [ ] The plugin's README says what it adds (themes, personas, Codex libraries).
- [ ] Persona prompts and Codex texts contain nothing I would not show to every user.
- [ ] Theme CSS loads nothing from the internet.
- [ ] The plugin has a license that allows redistribution.
- [ ] For an update: the CHANGELOG lists the changes since the pinned version.

### What reviewers check

The diff of the plugin repository between the old and the new pinned commit
(`https://github.com/<owner>/<repo>/compare/<old commit>...<new commit>`).
The commit pinned here is exactly what every user downloads.
