#!/bin/sh
# Finds a Python 3.10+ interpreter and hands over to tools/wisp_setup.py,
# which does the real work. Arguments are forwarded unchanged:
#   ./setup.sh            set up .venv, dependencies and checks
#   ./setup.sh --check    report on the setup without changing anything
#   ./setup.sh --dev      also install the test tools and run the suite
set -eu

cd "$(dirname "$0")"

for candidate in python3 python python3.14 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
            >/dev/null 2>&1; then
        exec "$candidate" tools/wisp_setup.py "$@"
    fi
done

echo "Wisp needs Python 3.10 or newer, and none was found." >&2
echo "Install it from https://www.python.org/downloads/ or your package manager," >&2
echo "for example: brew install python  /  sudo apt install python3 python3-venv" >&2
exit 1
