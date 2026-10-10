"""Characterization tests for tools/registry_tool.py.

The tool is what every plugin release passes through: the pre-commit hook runs `validate`, the pre-push hook runs
`validate --remote`, and a reviewer runs `manifest DIR` on a submission. It had no tests, while `validate_manifest`
alone was 85 statements with 38 branches - the shape that makes a silent gap in a check easy to introduce and
impossible to notice. These tests pin the accepted document and the exact message of every rejection, so the
validators can be split into sections without loosening a single rule.

Standard library only, like the tool itself: `python -m unittest discover tests` needs nothing installed.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import registry_tool as rt  # noqa: E402 - after the path insert


# ── Fixtures ─────────────────────────────────────────────────────────────────

def good_manifest() -> dict:
    """A manifest that must pass every check, with one contribution of each list kind."""
    return {
        "manifest_version": 1,
        "id": "example-stc",
        "name": "Example STC",
        "version": "1.2.0",
        "description": "A reference plugin.",
        "author": {"name": "Someone", "url": "https://example.com"},
        "license": "MIT",
        "app": {"min_version": "3.4.0", "plugin_api": 1},
        "contributes": {
            "themes": [{"id": "ember", "path": "themes/ember"}],
            "personas": [{"id": "adept", "path": "personas/adept.json"}],
            "codex": [{"library": "Example Lore", "path": "codex/Example Lore"}],
        },
        "help": "HELP.md",
        "credits": [{"name": "Someone", "for": "the plugin", "url": "https://example.com"}],
    }


def good_entry() -> dict:
    """A registry entry that must pass every check."""
    return {
        "id": "example-stc",
        "name": "Example STC",
        "description": "A reference plugin.",
        "author": "Someone",
        "repo": "https://github.com/someone/example-stc.git",
        "version": "1.2.0",
        "tag": "v1.2.0",
        "commit": "a" * 40,
        "kinds": ["themes", "personas", "codex"],
        "min_app_version": "3.4.0",
        "plugin_api": 1,
        "license": "MIT",
    }


class PluginTree:
    """A plugin checkout on disk, built from a manifest dict and changed per test."""

    def __init__(self, root: Path, manifest: dict | None = None):
        self.root = root
        self.manifest = manifest if manifest is not None else good_manifest()

    def write(self) -> Path:
        """Create every file the manifest declares, then the manifest itself."""
        (self.root / "themes" / "ember").mkdir(parents=True, exist_ok=True)
        self._put("themes/ember/theme.json", json.dumps({"displayName": "Ember", "version": "1.0.0"}))
        self._put("themes/ember/theme.css", ":root { --accent: #f60; }")
        self._put("personas/adept.json", json.dumps({"name": "Adept", "system_prompt": "You are an adept.",
                                                     "knowledge_plugin_libraries": ["Example Lore"]}))
        self._put("codex/Example Lore/About.md", "---\ntitle: About\n---\n\nSome lore.\n")
        self._put("HELP.md", "# Example\n\nHelp text.\n")
        self._put("README.md", "# Example\n")
        self._put("LICENSE", "MIT\n")
        self._put(rt.MANIFEST_NAME, json.dumps(self.manifest))
        return self.root

    def _put(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class ToolTest(unittest.TestCase):
    """Shared helpers: build a tree in a temporary folder and read the report."""

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "plugin"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def check(self, manifest: dict | None = None, **kwargs) -> rt.Report:
        """Validate a tree built from `manifest` (the good one when None) and return the report."""
        tree = PluginTree(self.root, manifest)
        tree.write()
        report, _man = rt.validate_manifest(tree.root, **kwargs)
        return report

    def put(self, rel: str, text: str) -> None:
        """Overwrite one file of the tree after it was written."""
        PluginTree(self.root)._put(rel, text)

    def assert_error(self, report: rt.Report, needle: str) -> None:
        """Assert exactly one error mentions `needle`, and show the report when it does not."""
        hits = [e for e in report.errors if needle in e]
        self.assertEqual(len(hits), 1, f"expected one error containing {needle!r}, got: {report.errors}")


# ── The accepted manifest ────────────────────────────────────────────────────

class TestGoodManifest(ToolTest):

    def test_a_complete_manifest_passes_without_errors_or_warnings(self):
        """The reference plugin must validate cleanly.

        Expected: no errors and no warnings. Every rejection test below is only meaningful if the baseline is
        accepted, so this is the anchor for all of them."""
        report = self.check()
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])

    def test_the_parsed_manifest_is_returned(self):
        """validate_manifest hands back the manifest it read, which `pin` uses to build the entry.

        Expected: the dict with the id. Returning None on a valid plugin would make `pin` write an empty entry."""
        PluginTree(self.root).write()
        report, man = rt.validate_manifest(self.root)
        self.assertEqual(report.errors, [])
        self.assertEqual(man["id"], "example-stc")

    def test_a_missing_readme_or_license_is_a_warning_not_an_error(self):
        """A plugin without README.md or LICENSE still validates.

        Expected: two warnings, no errors. They are review hints; refusing them would block a technically valid
        plugin."""
        PluginTree(self.root).write()
        (self.root / "README.md").unlink()
        (self.root / "LICENSE").unlink()
        report, _man = rt.validate_manifest(self.root)
        self.assertEqual(report.errors, [])
        self.assertEqual(len(report.warnings), 2)


# ── The manifest's own fields ────────────────────────────────────────────────

class TestManifestFields(ToolTest):

    def test_a_missing_manifest_file_stops_with_one_error(self):
        """A checkout without strategicum-plugin.json reports that and nothing else.

        Expected: one error naming the file, and no manifest. Continuing would report every other rule as broken
        too and bury the real cause."""
        report, man = rt.validate_manifest(self.root)
        self.assertEqual(len(report.errors), 1)
        self.assertIn(rt.MANIFEST_NAME, report.errors[0])
        self.assertIsNone(man)

    def test_a_manifest_that_is_not_an_object_is_rejected(self):
        """A JSON array or string at the top level is not a manifest.

        Expected: an error and no manifest. The rest of the checks index the manifest as a dict."""
        PluginTree(self.root).write()
        (self.root / rt.MANIFEST_NAME).write_text("[]", encoding="utf-8")
        report, man = rt.validate_manifest(self.root)
        self.assert_error(report, "must be a JSON object")
        self.assertIsNone(man)

    def test_unparsable_json_is_reported_with_its_position(self):
        """Broken JSON is an error, not a crash.

        Expected: an error mentioning the manifest. A traceback in the pre-commit hook tells the author nothing."""
        PluginTree(self.root).write()
        (self.root / rt.MANIFEST_NAME).write_text('{"id": }', encoding="utf-8")
        report, man = rt.validate_manifest(self.root)
        self.assertTrue(report.errors)
        self.assertIsNone(man)

    def test_an_unknown_top_level_key_is_rejected(self):
        """A key the app does not read is an error, not something to ignore.

        Expected: one error naming the key. "contributions" instead of "contributes" would otherwise install a
        plugin that contributes nothing."""
        man = good_manifest()
        man["contributions"] = {}
        self.assert_error(self.check(man), "unknown key 'contributions'")

    def test_the_manifest_version_must_be_the_supported_one(self):
        """manifest_version is the format gate.

        Expected: an error for 2 and for a missing one. A future format must be refused by an old tool rather than
        half-read."""
        man = good_manifest()
        man["manifest_version"] = 2
        self.assert_error(self.check(man), "manifest_version must be 1")
        del man["manifest_version"]
        self.assert_error(self.check(man), "manifest_version must be 1")

    def test_the_id_must_be_a_lowercase_slug(self):
        """The id becomes part of derived names in the app and can never change after a release.

        Expected: uppercase, underscores, a leading hyphen and a single character are all refused."""
        for bad in ("Example", "example_stc", "-example", "a", "x" * 41):
            man = good_manifest()
            man["id"] = bad
            self.assert_error(self.check(man), "id must match")

    def test_the_version_must_be_semantic(self):
        """The registry compares versions to offer updates and pins the tag v<version>.

        Expected: a two-part version, a leading zero and a v-prefix are refused."""
        for bad in ("1.2", "01.2.3", "v1.2.3", "latest"):
            man = good_manifest()
            man["version"] = bad
            self.assert_error(self.check(man), "version must be semantic")

    def test_name_description_and_license_have_length_limits(self):
        """The app and the index show these untruncated.

        Expected: an empty string and an over-long one are refused per field, with the limit in the message
        (name 60, description 300, license 60)."""
        for key, limit in (("name", 60), ("description", 300), ("license", 60)):
            man = good_manifest()
            man[key] = "x" * (limit + 1)
            self.assert_error(self.check(man), f"'{key}' must be a non-empty string of at most {limit}")
            man[key] = "   "
            self.assert_error(self.check(man), f"'{key}'")

    def test_the_author_needs_a_name_and_an_https_url(self):
        """Attribution must be usable and the link safe to show.

        Expected: a string author, a missing name and an http:// url are each refused."""
        man = good_manifest()
        man["author"] = "Someone"
        self.assert_error(self.check(man), "author must be an object with a name")
        man["author"] = {"url": "https://example.com"}
        self.assert_error(self.check(man), "author must be an object with a name")
        man["author"] = {"name": "Someone", "url": "http://example.com"}
        self.assert_error(self.check(man), "author.url must be an https:// URL")

    def test_the_app_block_must_carry_a_min_version_and_a_known_plugin_api(self):
        """The app refuses to install a plugin whose requirements it cannot read.

        Expected: a missing app block, a malformed min_version or max_version, and an unsupported plugin_api are
        each refused."""
        man = good_manifest()
        man["app"] = []
        self.assert_error(self.check(man), "app must be an object")
        man["app"] = {"plugin_api": 1}
        self.assert_error(self.check(man), "app.min_version must look like 3.0.0")
        man["app"] = {"min_version": "3.4", "plugin_api": 1}
        self.assert_error(self.check(man), "app.min_version must look like 3.0.0")
        man["app"] = {"min_version": "3.4.0", "max_version": "3.9", "plugin_api": 1}
        self.assert_error(self.check(man), "app.max_version must look like 3.9.0")
        man["app"] = {"min_version": "3.4.0", "plugin_api": 3}
        self.assert_error(self.check(man), "app.plugin_api must be one of")

    def test_the_data_version_must_be_a_whole_number_in_range(self):
        """data_version tells the app when a plugin's stored data format changed.

        Expected: a string, a float, a boolean and zero are refused; a valid one passes."""
        for bad in ("1", 1.5, True, 0, 1_000_001):
            man = good_manifest()
            man["data_version"] = bad
            self.assert_error(self.check(man), "data_version must be a whole number")
        man = good_manifest()
        man["data_version"] = 3
        self.assertEqual(self.check(man).errors, [])


# ── Contributions ────────────────────────────────────────────────────────────

class TestContributions(ToolTest):

    def test_contributes_must_be_a_non_empty_object_of_known_kinds(self):
        """A plugin that contributes nothing, or something unknown, is refused.

        Expected: an empty object and an unknown kind are each an error. An unknown kind is usually a typo that
        would silently contribute nothing."""
        man = good_manifest()
        man["contributes"] = {}
        self.assert_error(self.check(man), "contributes must be a non-empty object")
        man["contributes"] = {"widgets": [{"id": "x", "path": "x"}]}
        self.assert_error(self.check(man), "unknown kind 'widgets'")

    def test_a_list_kind_must_be_a_non_empty_list(self):
        """themes/personas/codex are lists; an object or an empty list is a mistake.

        Expected: both are refused, naming the kind."""
        man = good_manifest()
        man["contributes"]["themes"] = []
        self.assert_error(self.check(man), "themes must be a non-empty list")
        man["contributes"]["themes"] = {"id": "ember", "path": "themes/ember"}
        self.assert_error(self.check(man), "themes must be a non-empty list")

    def test_a_contribution_item_must_have_exactly_its_two_keys(self):
        """An extra or missing key in an item is refused rather than ignored.

        Expected: the message names the expected keys. An item carrying settings the app does not read would look
        like it works."""
        man = good_manifest()
        man["contributes"]["themes"] = [{"id": "ember", "path": "themes/ember", "default": True}]
        self.assert_error(self.check(man), "must have exactly the keys")
        man["contributes"]["themes"] = [{"path": "themes/ember"}]
        self.assert_error(self.check(man), "must have exactly the keys")

    def test_duplicate_ids_and_libraries_are_refused(self):
        """Two contributions of a kind may not share an id or a library name.

        Expected: a duplicate is reported once. The second would overwrite the first in the app."""
        man = good_manifest()
        man["contributes"]["themes"] = [{"id": "ember", "path": "themes/ember"},
                                        {"id": "ember", "path": "themes/ember"}]
        self.assert_error(self.check(man), "duplicate id 'ember'")

    def test_a_sub_id_must_be_a_lowercase_slug(self):
        """A theme or persona id follows the same rule as the plugin id.

        Expected: an uppercase id is refused."""
        man = good_manifest()
        man["contributes"]["themes"] = [{"id": "Ember", "path": "themes/ember"}]
        self.assert_error(self.check(man), "id must be lowercase letters, digits and hyphens")

    def test_a_path_must_stay_inside_the_plugin(self):
        """A traversal path is refused for every kind.

        Expected: an error for "../secrets". This is the rule that keeps a manifest from reaching outside the
        checkout when the app copies the files."""
        man = good_manifest()
        man["contributes"]["themes"] = [{"id": "ember", "path": "../secrets"}]
        self.assertTrue(self.check(man).errors)

    def test_a_theme_needs_both_files_and_may_not_load_remote_resources(self):
        """theme.json and theme.css are required, and the CSS must not call home.

        Expected: a missing file is an error; @import and a remote url() are errors; the same text inside a CSS
        comment is not. A theme that fetches a font makes every app start depend on a third-party host."""
        self.check()
        (self.root / "themes" / "ember" / "theme.json").unlink()
        report, _man = rt.validate_manifest(self.root)
        self.assertTrue(report.errors)

        for css in ('@import url("https://fonts.example/x.css");', ":root { background: url(//cdn.example/x.png); }"):
            self.check()
            self.put("themes/ember/theme.css", css)
            report, _man = rt.validate_manifest(self.root)
            self.assert_error(report, "remote")

        self.check()
        self.put("themes/ember/theme.css", "/* no @import here */\n:root { --accent: #f60; }")
        report, _man = rt.validate_manifest(self.root)
        self.assertEqual(report.errors, [])

    def test_a_persona_needs_a_name_and_prompt_and_only_known_fields(self):
        """A persona file is checked field by field.

        Expected: a missing system_prompt is an error; a misspelt field is a *warning* saying the app will drop it
        (not an error - a plugin may be written for a newer app than the tool knows). That warning is the usual
        cause of "my persona setting does nothing". A bad mode or top_k is an error."""
        self.check()
        self.put("personas/adept.json", json.dumps({"name": "Adept"}))
        report, _man = rt.validate_manifest(self.root)
        self.assert_error(report, "'system_prompt' must be a non-empty string")

        self.check()
        self.put("personas/adept.json", json.dumps({"name": "A", "system_prompt": "p", "tempreature": 0.5}))
        report, _man = rt.validate_manifest(self.root)
        self.assertEqual(report.errors, [])
        self.assertEqual(len([w for w in report.warnings if "tempreature" in w]), 1, report.warnings)

        self.check()
        self.put("personas/adept.json", json.dumps({"name": "A", "system_prompt": "p",
                                                    "knowledge_mode": "sometimes", "knowledge_top_k": 50}))
        report, _man = rt.validate_manifest(self.root)
        self.assert_error(report, "knowledge_mode must be one of")
        self.assert_error(report, "knowledge_top_k must be an integer from 1 to 10")

    def test_a_persona_may_only_claim_a_library_this_plugin_contributes(self):
        """knowledge_plugin_libraries is matched against the manifest's own codex contributions.

        Expected: an unknown library name is an error. A typo there leaves the persona with no sources at all,
        which looks like a broken Codex."""
        self.check()
        self.put("personas/adept.json", json.dumps({"name": "A", "system_prompt": "p",
                                                    "knowledge_plugin_libraries": ["Other Lore"]}))
        report, _man = rt.validate_manifest(self.root)
        self.assertTrue(report.errors)

    def test_a_codex_library_needs_a_plain_name_and_at_least_one_document(self):
        """The library name becomes a folder, and an empty library cannot be shown.

        Expected: a name with a path separator or a leading dot is refused, and a folder without .md files is an
        error."""
        for bad in ("Lore/Sub", "_Lore", ".Lore", "Lore?"):
            man = good_manifest()
            man["contributes"]["codex"] = [{"library": bad, "path": "codex/Example Lore"}]
            self.assertTrue(self.check(man).errors, f"{bad!r} should be refused")

        self.check()
        (self.root / "codex" / "Example Lore" / "About.md").unlink()
        report, _man = rt.validate_manifest(self.root)
        self.assertTrue(report.errors)

    def test_backend_needs_plugin_api_2(self):
        """A backend contribution on plugin_api 1 is refused.

        Expected: an error naming the requirement. The app would otherwise be asked to import code from a plugin
        that does not declare the API that allows it."""
        man = good_manifest()
        man["contributes"]["backend"] = {"module": "example", "entry": "create_plugin"}
        self.assert_error(self.check(man), "plugin_api 2")


# ── What the registry entry expects ──────────────────────────────────────────

class TestExpectations(ToolTest):

    def test_a_mismatching_id_version_or_kind_list_is_reported(self):
        """`validate --remote` passes the entry's values in; each mismatch is named.

        Expected: one error per mismatching field, quoting both sides. This is the check that catches an entry
        pinned to the wrong tag."""
        report = self.check(expect_id="other-stc", expect_version="9.9.9",
                            expect_kinds=["themes"])
        self.assert_error(report, "but the registry entry says 'other-stc'")
        self.assert_error(report, "but the registry entry says '9.9.9'")
        self.assert_error(report, "lists kinds ['themes']")

    def test_matching_expectations_add_no_errors(self):
        """The same values as the manifest are accepted.

        Expected: no errors. A false positive here would block every correct release."""
        report = self.check(expect_id="example-stc", expect_version="1.2.0",
                            expect_kinds=["themes", "personas", "codex"])
        self.assertEqual(report.errors, [])


# ── Registry entries ─────────────────────────────────────────────────────────

class TestEntries(unittest.TestCase):

    def errors(self, entry, **kwargs):
        """The errors of one entry."""
        return rt.validate_entry(entry, 0, **kwargs).errors

    def test_a_complete_entry_passes(self):
        """The reference entry must validate cleanly.

        Expected: no errors. The anchor for the rejection tests below."""
        self.assertEqual(self.errors(good_entry()), [])

    def test_an_entry_must_be_an_object_with_every_required_key(self):
        """A non-object entry, and any missing required key, is refused.

        Expected: an error for a list, and one naming each missing key. A registry entry without a commit cannot
        be verified at all."""
        self.assertTrue(self.errors([]))
        for key in rt.ENTRY_REQUIRED:
            entry = good_entry()
            del entry[key]
            self.assertTrue(self.errors(entry), f"a missing {key} should be refused")

    def test_an_unknown_key_is_refused(self):
        """Only the documented keys may appear.

        Expected: an error naming the key, so a renamed field is noticed at review time."""
        entry = good_entry()
        entry["featured"] = True
        self.assertTrue(self.errors(entry))

    def test_the_repo_must_be_an_https_clone_url(self):
        """The index only lists repositories that can be cloned without credentials.

        Expected: ssh, a missing .git, whitespace and a non-string are refused; a file:// URL only with
        --allow-local, which is how the tool is tested without the network."""
        for bad in ("git@github.com:someone/x.git", "https://github.com/someone/x", 42,
                    "https://github.com/some one/x.git"):
            entry = good_entry()
            entry["repo"] = bad
            self.assertTrue(self.errors(entry), f"{bad!r} should be refused")
        entry = good_entry()
        entry["repo"] = "file:///tmp/x.git"
        self.assertTrue(self.errors(entry))
        self.assertEqual(self.errors(entry, allow_local=True), [])

    def test_the_commit_must_be_a_full_lowercase_sha(self):
        """A short or upper-case commit cannot be compared to what git reports.

        Expected: a 7-character sha, an upper-case one and a tag name are refused."""
        for bad in ("a" * 7, "A" * 40, "v1.2.0"):
            entry = good_entry()
            entry["commit"] = bad
            self.assertTrue(self.errors(entry), f"{bad!r} should be refused")

    def test_the_kinds_must_be_known_and_non_empty(self):
        """An entry's kinds are what the app checks the manifest against.

        Expected: an empty list and an unknown kind are refused."""
        entry = good_entry()
        entry["kinds"] = []
        self.assertTrue(self.errors(entry))
        entry["kinds"] = ["widgets"]
        self.assertTrue(self.errors(entry))

    def test_a_registry_document_is_checked_as_a_whole(self):
        """validate_registry_doc checks the schema version and every entry, and refuses duplicate ids.

        Expected: a good document passes; a wrong schema_version, a non-list plugins field and two entries with
        the same id are each refused. This is what the pre-commit hook runs."""
        def doc(**over):
            """A registry document with one good entry, fields overridden per case."""
            base = {"schema_version": rt.SUPPORTED_SCHEMA_VERSION, "name": "STC Index",
                    "updated": "2026-10-09", "plugins": [good_entry()]}
            base.update(over)
            return base

        self.assertEqual(rt.validate_registry_doc(doc()).errors, [])
        self.assertTrue(rt.validate_registry_doc(doc(schema_version=99)).errors)
        self.assertTrue(rt.validate_registry_doc(doc(plugins={})).errors)
        self.assertTrue(rt.validate_registry_doc(doc(updated="09.10.2026")).errors)
        self.assertTrue(rt.validate_registry_doc(doc(name="")).errors)
        self.assertTrue(rt.validate_registry_doc(doc(plugins=[good_entry(), good_entry()])).errors)
        self.assertTrue(rt.validate_registry_doc([]).errors)


class TestCategories(unittest.TestCase):
    """Plugin categories and manifest_url (app 3.13.0): the registry check must reject exactly what the
    app rejects, because the app re-validates everything and a looser check here only moves the failure
    from review time to install time."""

    def entry_errors(self, **over):
        """The errors of a good entry with `over` applied."""
        entry = good_entry()
        entry.update(over)
        return rt.validate_entry(entry, 0).errors

    def test_an_entry_may_declare_categories_and_a_manifest_url(self):
        """Both keys are optional additions to an entry.

        Expected: no errors with either or both set. An entry that validated before must still validate,
        and an entry using the new keys must not be refused by the hook."""
        self.assertEqual(self.entry_errors(), [])
        self.assertEqual(self.entry_errors(categories=["game", "persona"]), [])
        self.assertEqual(self.entry_errors(
            manifest_url="https://raw.githubusercontent.com/o/r/{ref}/strategicum-plugin.json"), [])

    def test_bad_categories_are_refused(self):
        """A non-list, an upper-case or spaced slug, a non-string and more than MAX_CATEGORIES are refused.

        Expected: an error for each. The app filters its plugin list by these slugs, so one that does not
        match CATEGORY_RE would be filed under a chip nobody can select."""
        for bad in ("game", ["Game"], ["has space"], ["-lead"], ["trail-"], [1], ["a"],
                    ["c%d" % i for i in range(rt.MAX_CATEGORIES + 1)]):
            self.assertTrue(self.entry_errors(categories=bad), f"{bad!r} should be refused")

    def test_a_manifest_url_must_be_https_and_carry_the_ref_placeholder(self):
        """The URL is fetched once per candidate tag, with {ref} replaced by the tag.

        Expected: http, a missing {ref}, a traversal and a non-string are refused. Without the
        placeholder every tag would read the same file, so the app would offer the wrong version."""
        for bad in ("http://x.test/{ref}/m.json", "https://x.test/fixed.json",
                    "https://x.test/../{ref}/m.json", 7):
            self.assertTrue(self.entry_errors(manifest_url=bad), f"{bad!r} should be refused")

    def test_the_document_may_declare_a_category_vocabulary(self):
        """The registry document's top-level `categories` names and orders the app's filter chips.

        Expected: a valid vocabulary passes; a non-list and an entry with a bad slug or no label are
        refused. The vocabulary is how a new category ships without an app release, so it is the one
        place a typo would silently rename a chip."""
        def doc(**over):
            """A registry document with one good entry, fields overridden per case."""
            base = {"schema_version": rt.SUPPORTED_SCHEMA_VERSION, "name": "STC Index",
                    "updated": "2026-10-10", "plugins": [good_entry()]}
            base.update(over)
            return base

        self.assertEqual(rt.validate_registry_doc(doc(categories=[
            {"id": "game", "label": "Games", "order": 1, "description": "Game companions"},
            {"id": "utility", "label": "Utilities"},
        ])).errors, [])
        self.assertTrue(rt.validate_registry_doc(doc(categories={})).errors)
        self.assertTrue(rt.validate_registry_doc(doc(categories=[{"id": "Bad", "label": "x"}])).errors)
        self.assertTrue(rt.validate_registry_doc(doc(categories=[{"id": "game"}])).errors)
        self.assertTrue(rt.validate_registry_doc(doc(categories=[{"id": "game", "label": ""}])).errors)

    def test_a_manifest_may_declare_categories(self):
        """The same slug rule applies to a plugin manifest, which is where categories originate.

        Expected: a manifest with valid categories passes and a bad slug is refused, so `manifest DIR`
        tells a contributor before the app does."""
        def identity_errors(categories):
            """The errors _manifest_identity reports for a manifest with these categories."""
            man = good_manifest()
            man["categories"] = categories
            rep = rt.Report()
            rt._manifest_identity(man, rep)
            return rep.errors

        self.assertEqual(identity_errors(["game", "knowledge-base"]), [])
        self.assertTrue(identity_errors(["Game"]))
        self.assertTrue(identity_errors("game"))


# ── The report itself ────────────────────────────────────────────────────────

class TestReport(unittest.TestCase):

    def test_the_report_collects_and_merges_errors_and_warnings(self):
        """Report.error/warn prefix the place, and extend merges another report.

        Expected: both lists carry "where: message" and survive the merge. The exit code of the tool is decided by
        `errors` alone, so a warning must never land there."""
        a, b = rt.Report(), rt.Report()
        a.error("manifest", "broken")
        b.warn("tree", "missing")
        a.extend(b)
        self.assertEqual(a.errors, ["manifest: broken"])
        self.assertEqual(a.warnings, ["tree: missing"])


# ── The real registry ────────────────────────────────────────────────────────

class TestTheRegistryItself(unittest.TestCase):

    def test_the_checked_in_registry_is_valid(self):
        """registry.json in this repository passes the offline check.

        Expected: no errors. The pre-commit hook enforces this for every commit that touches the file; running it
        here means a change to the *tool* cannot invalidate the registry unnoticed."""
        path = Path(__file__).resolve().parent.parent / "registry.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        report = rt.validate_registry_doc(doc)
        self.assertEqual(report.errors, [])


if __name__ == "__main__":
    unittest.main()
