#!/usr/bin/env sh
# Installs the `agh` command for the current user on macOS and Linux.
#   ./install.sh                      # from a checkout of this repo
#   ./install.sh <path-or-git-url>    # from elsewhere
#   AGH_EDITABLE=1 ./install.sh       # editable install for development
# Uses uv if present, then pipx, then a private venv in ~/.agh/venv.
set -eu
SOURCE="${1:-$(cd "$(dirname "$0")" && pwd)}"
EDIT=""
[ "${AGH_EDITABLE:-}" = "1" ] && EDIT="--editable"

if command -v uv >/dev/null 2>&1; then
  echo "Installing with uv..."
  # --reinstall --refresh: always build from the current source, never a cached older build.
  if [ -n "$EDIT" ]; then uv tool install --force $EDIT "$SOURCE"
  else uv tool install --force --reinstall --refresh "$SOURCE"; fi
  uv tool update-shell >/dev/null 2>&1 || true
elif command -v pipx >/dev/null 2>&1; then
  echo "Installing with pipx..."
  pipx install --force $EDIT --pip-args="--no-cache-dir" "$SOURCE"
  pipx ensurepath >/dev/null 2>&1 || true
else
  PY="$(command -v python3 || command -v python || true)"
  [ -n "$PY" ] || { echo "Python 3.11+ is required" >&2; exit 1; }
  HOME_DIR="${AGH_HOME:-$HOME/.agh}"
  echo "Installing into $HOME_DIR/venv..."
  "$PY" -m venv "$HOME_DIR/venv"
  "$HOME_DIR/venv/bin/python" -m pip install --upgrade pip >/dev/null
  if [ -n "$EDIT" ]; then "$HOME_DIR/venv/bin/python" -m pip install -e "$SOURCE"
  else "$HOME_DIR/venv/bin/python" -m pip install --upgrade --force-reinstall --no-cache-dir "$SOURCE"; fi
  mkdir -p "$HOME/.local/bin"
  ln -sf "$HOME_DIR/venv/bin/agh" "$HOME/.local/bin/agh"
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Add ~/.local/bin to your PATH." ;; esac
fi

echo
echo "Installed. Open a new terminal, then:"
echo "  cd <any folder> && agh     # that folder becomes the workspace"
echo "  The first start asks which provider to use and tests your key (run /setup later to change it)."
