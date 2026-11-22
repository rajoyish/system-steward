#!/bin/sh
# System Steward installer for macOS.
#
#   curl -fsSL https://rajoyish.github.io/system-steward/install.sh | sh
#   curl -fsSL https://rajoyish.github.io/system-steward/install.sh | sh -s -- --uninstall
#
# Installs the latest release into ~/.local/share/system-steward (app + isolated Python
# environment) and a `system-steward` launcher into ~/.local/bin. No administrator rights needed.
#
# Environment overrides:
#   SYSTEM_STEWARD_VERSION   release to install, e.g. 3.0.0 (default: latest)
#   SYSTEM_STEWARD_HOME      install folder        (default: ~/.local/share/system-steward)
#   SYSTEM_STEWARD_BIN       launcher folder       (default: ~/.local/bin)
#   SYSTEM_STEWARD_PYTHON    Python 3.10+ to use   (default: newest python3.x found)
set -eu

REPO="rajoyish/system-steward"
HOME_DIR="${SYSTEM_STEWARD_HOME:-$HOME/.local/share/system-steward}"
BIN_DIR="${SYSTEM_STEWARD_BIN:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/system-steward"

say() { printf '%s\n' "$*"; }
fail() { printf 'system-steward install: %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$HOME_DIR"
    if [ -f "$LAUNCHER" ] && grep -q "system-steward" "$LAUNCHER" 2>/dev/null; then
        rm -f "$LAUNCHER"
    fi
    say "System Steward removed."
    exit 0
fi

[ "$(uname -s)" = "Darwin" ] || fail "this installer supports macOS. On Windows use: irm https://rajoyish.github.io/system-steward/install.ps1 | iex"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v tar >/dev/null 2>&1 || fail "tar is required"

# Python 3.10 or newer that can actually build an environment: a broken interpreter (for example
# one whose pyexpat or ssl no longer links against the system libraries) is skipped.
python_ok() {
    "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1 &&
        "$1" -c 'import ensurepip, pyexpat, ssl, venv' >/dev/null 2>&1
}
PYTHON="${SYSTEM_STEWARD_PYTHON:-}"
if [ -n "$PYTHON" ]; then
    python_ok "$PYTHON" || fail "$PYTHON is not a working Python 3.10 or newer"
else
    for candidate in python3.16 python3.15 python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if python_ok "$candidate"; then
            PYTHON="$(command -v "$candidate")"
            break
        fi
        if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
            BROKEN="${BROKEN:-} $(command -v "$candidate")"
        fi
    done
fi
if [ -z "$PYTHON" ] && [ -n "${BROKEN:-}" ]; then
    fail "found Python 3.10+ at${BROKEN} but it cannot import ssl/pyexpat/venv, so it cannot install packages. Reinstall or update it (for Homebrew: brew update && brew upgrade), or set SYSTEM_STEWARD_PYTHON to a working python3."
fi
[ -n "$PYTHON" ] || fail "Python 3.10 or newer is required. Install it with 'brew install python' or from https://www.python.org/downloads/"

# Resolve the release.
VERSION="${SYSTEM_STEWARD_VERSION:-}"
if [ -z "$VERSION" ]; then
    latest="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest")" \
        || fail "could not reach GitHub"
    VERSION="${latest##*/v}"
    case "$VERSION" in
        *[!0-9.]* | "") fail "could not determine the latest release (got '$latest')" ;;
    esac
fi
VERSION="${VERSION#v}"
ARCHIVE="system-steward-$VERSION.tar.gz"
BASE="${SYSTEM_STEWARD_RELEASE_URL:-https://github.com/$REPO/releases/download/v$VERSION}"  # override for testing

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

say "==> Downloading System Steward $VERSION"
curl -fsSL "$BASE/$ARCHIVE" -o "$TMP/$ARCHIVE" || fail "download failed: $BASE/$ARCHIVE"
curl -fsSL "$BASE/checksums.txt" -o "$TMP/checksums.txt" || fail "download failed: $BASE/checksums.txt"
expected="$(awk -v f="$ARCHIVE" '$2 == f || $2 == "*" f { print $1 }' "$TMP/checksums.txt")"
actual="$(shasum -a 256 "$TMP/$ARCHIVE" | awk '{ print $1 }')"
[ -n "$expected" ] && [ "$expected" = "$actual" ] || fail "checksum mismatch for $ARCHIVE"

say "==> Installing into $HOME_DIR"
mkdir -p "$HOME_DIR" "$BIN_DIR"
tar -xzf "$TMP/$ARCHIVE" -C "$TMP"
rm -rf "$HOME_DIR/app.new"
mv "$TMP/system-steward-$VERSION" "$HOME_DIR/app.new"

# Recreate the environment when the interpreter changed; otherwise reuse it.
if [ ! -x "$HOME_DIR/venv/bin/python" ] || [ "$(cat "$HOME_DIR/venv/.python" 2>/dev/null)" != "$PYTHON" ]; then
    rm -rf "$HOME_DIR/venv"
    "$PYTHON" -m venv "$HOME_DIR/venv" || fail "could not create a Python environment with $PYTHON"
    printf '%s\n' "$PYTHON" > "$HOME_DIR/venv/.python"
fi
"$HOME_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    -r "$HOME_DIR/app.new/requirements.txt" || fail "installing Python requirements failed"

rm -rf "$HOME_DIR/app"
mv "$HOME_DIR/app.new" "$HOME_DIR/app"

cat > "$LAUNCHER" <<EOF
#!/bin/sh
# system-steward launcher (installed by install.sh)
exec "$HOME_DIR/venv/bin/python" "$HOME_DIR/app/system_steward.py" "\$@"
EOF
chmod 755 "$LAUNCHER"

"$LAUNCHER" --version >/dev/null || fail "the installed launcher did not start"
say "==> Installed $("$LAUNCHER" --version)"

case ":$PATH:" in
    *":$BIN_DIR:"*) say "Run: system-steward" ;;
    *)
        say ""
        say "$BIN_DIR is not on your PATH. Add it, then open a new terminal:"
        say "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc"
        say "Or run it directly: $LAUNCHER"
        ;;
esac
