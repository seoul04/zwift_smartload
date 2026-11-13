#!/usr/bin/env bash
# Build the dongle firmware with the NCS toolchain environment set up.
#
# Why this exists: the NCS toolchain bundles its own python3.12, ccache and
# Zephyr SDK, but none of them are on PATH by default. After the system Python
# moved to 3.14, a bare `ninja -C build` fails in two confusing ways:
#
#   env: 'python3.12': No such file or directory
#   python3.12: error while loading shared libraries: libpython3.12.so.1.0
#   /bin/sh: line 1: ccache: command not found
#
# The first two are the same problem: the toolchain's interpreter exists but its
# shared library is not on the loader path. The third is ccache living in the
# toolchain's usr/bin rather than usr/local/bin. Both are fixed here.
#
# Usage:
#   ./build.sh              # incremental build
#   ./build.sh pristine     # wipe the build tree and reconfigure
#   ./build.sh flash        # build, then flash with nrfutil (see below)

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$APP_DIR/build"

# Locate the NCS toolchain. Prefer an explicit override, then the newest
# installed one, so this keeps working when the toolchain is upgraded.
if [[ -n "${NCS_TOOLCHAIN:-}" ]]; then
    TC="$NCS_TOOLCHAIN"
else
    TC="$(find "$HOME/tools/ncs/toolchains" -maxdepth 1 -mindepth 1 -type d \
          -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"
fi

if [[ -z "${TC:-}" || ! -d "$TC" ]]; then
    echo "error: NCS toolchain not found under ~/tools/ncs/toolchains" >&2
    echo "       set NCS_TOOLCHAIN=/path/to/toolchain to override" >&2
    exit 1
fi

# ccache is in usr/bin; the toolchain python and cmake are in usr/local/bin.
export PATH="$TC/usr/bin:$TC/usr/local/bin:$PATH"
# libpython3.12.so.1.0 lives in the toolchain's own lib dir.
export LD_LIBRARY_PATH="$TC/usr/local/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# Fail early with a clear message rather than a wall of ninja output.
if ! python3.12 -c 'import sys' 2>/dev/null; then
    echo "error: toolchain python3.12 cannot start" >&2
    echo "       tried: $TC/usr/local/bin/python3.12" >&2
    exit 1
fi
if ! command -v ccache >/dev/null 2>&1; then
    echo "error: ccache not found in the toolchain" >&2
    exit 1
fi

case "${1:-build}" in
    pristine)
        echo "wiping $BUILD_DIR"
        rm -rf "$BUILD_DIR"
        echo "reconfigure with: west build -b nrf52840dongle/nrf52840 $APP_DIR" >&2
        exit 0
        ;;
    flash)
        ninja -C "$BUILD_DIR"
        echo
        echo "built: $BUILD_DIR/merged.hex"
        echo "flash with the Movesense programmer or:"
        echo "  nrfutil device program --firmware $BUILD_DIR/merged.hex"
        exit 0
        ;;
    build|"")
        ninja -C "$BUILD_DIR"
        ;;
    *)
        echo "usage: $0 [build|pristine|flash]" >&2
        exit 2
        ;;
esac
