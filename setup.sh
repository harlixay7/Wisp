#!/bin/sh
# Finds Python 3.10+ (offering to install it when there is none) and hands
# over to tools/wisp_setup.py. Arguments are forwarded unchanged:
#   ./setup.sh              install and verify Wisp
#   ./setup.sh --connect    connect Claude Code, Codex or another assistant
#   ./setup.sh --check      report on the setup without changing anything
#   ./setup.sh --yes        accept every offer without asking
#   ./setup.sh --dev        also install the test tools and run the suite
#   ./setup.sh --widget     also install the Electron overlay (built for Windows)
set -eu

cd "$(dirname "$0")"

find_python() {
    for candidate in python3 python python3.14 python3.13 python3.12 python3.11 python3.10; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
                >/dev/null 2>&1; then
            PY=$candidate
            return 0
        fi
    done
    return 1
}

install_command() {
    sudo=""
    [ "$(id -u)" -eq 0 ] || sudo="sudo "
    if [ "$(uname)" = Darwin ] && command -v brew >/dev/null 2>&1; then
        echo "brew install python@3.12"
    elif command -v apt-get >/dev/null 2>&1; then
        echo "${sudo}apt-get install -y python3 python3-venv"
    elif command -v dnf >/dev/null 2>&1; then
        echo "${sudo}dnf install -y python3"
    elif command -v pacman >/dev/null 2>&1; then
        echo "${sudo}pacman -S --needed --noconfirm python"
    elif command -v zypper >/dev/null 2>&1; then
        echo "${sudo}zypper install -y python3"
    fi
}

PY=""
if ! find_python; then
    echo "Wisp needs Python 3.10 or newer, and none was found." >&2
    cmd=$(install_command)
    if [ -n "$cmd" ]; then
        answer=""
        for arg in "$@"; do
            case "$arg" in --yes | -y) answer=y ;; esac
        done
        if [ -z "$answer" ] && [ -t 0 ]; then
            printf 'Install it now (%s)? [Y/n] ' "$cmd"
            read -r answer || answer=n
            answer=${answer:-y}
        fi
        case "$answer" in
            y | Y | yes) sh -c "$cmd" && find_python || true ;;
        esac
    fi
fi

if [ -n "$PY" ]; then
    exec "$PY" tools/wisp_setup.py "$@"
fi

echo "Install Python 3.10 or newer from https://www.python.org/downloads/ or your" >&2
echo "package manager, then run ./setup.sh again." >&2
exit 1
