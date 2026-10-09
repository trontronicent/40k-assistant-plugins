"""The code shape this repository keeps: no function that does too much, takes too many arguments or hides a zip
length mismatch (`ruff.toml`, the same limits as the app, the plugin template and the No Man's Sky plugin). The
test runs ruff when it is installed (the 40k Assistant's venv has it) and skips otherwise, so a clone without it
still runs the rest of the suite - the tool itself needs only the standard library.

Keeps the 2026-10-09 split of `validate_entry`, `validate_manifest`, `_check_persona` and `_check_help_and_credits`
from growing back: every new rule is a section check, not one more branch in the function that is already there."""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _ruff() -> list[str] | None:
    """How to run ruff here: the module of this interpreter, else a ruff on the PATH, else None."""
    probe = subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True, text=True)
    if probe.returncode == 0:
        return [sys.executable, "-m", "ruff"]
    found = shutil.which("ruff")
    return [found] if found else None


class TestCodeShape(unittest.TestCase):

    def test_the_tooling_keeps_its_code_shape(self):
        """ruff (complexity <= 12, <= 50 statements, <= 12 branches, <= 6 arguments) reports nothing.

        Expected: exit 0 with no findings for tools/ and tests/. It matters because every new manifest rule wants
        to be one more branch in validate_manifest, which is how it reached 85 statements."""
        ruff = _ruff()
        if ruff is None:
            self.skipTest("ruff is not installed")
        res = subprocess.run([*ruff, "check", "tools", "tests", "--output-format", "concise"],
                             cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, "code shape findings:\n" + res.stdout)


if __name__ == "__main__":
    unittest.main()
