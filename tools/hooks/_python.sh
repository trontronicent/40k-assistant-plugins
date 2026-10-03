# Sourced by the hooks: pick a Python (any version >= 3.9; the tool uses only the standard library).
# Override with: git config stc.python "/path/to/python"
PY=$(git config --get stc.python)
if [ -z "$PY" ]; then
    for candidate in python3 python py; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import sys; sys.exit(sys.version_info < (3, 9))" >/dev/null 2>&1; then
            PY=$candidate
            break
        fi
    done
fi
if [ -z "$PY" ]; then
    echo "STC hook: no Python >= 3.9 found; set one with: git config stc.python /path/to/python" >&2
    exit 1
fi
