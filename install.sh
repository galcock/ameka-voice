#!/bin/bash
# Ameka Voice installer.  curl -fsSL https://ameka.ai/install.sh | bash
set -euo pipefail

VERSION="${AMEKA_VERSION:-0.16.16}"
BASE="${AMEKA_BASE:-https://ameka.ai}"
HOME_DIR="$HOME/.ameka"
APP_DIR="$HOME_DIR/app"
VENV="$HOME_DIR/venv"
BIN_DIR="$HOME/.local/bin"

say() { printf "\033[38;5;80m▍\033[0m %s\n" "$1"; }
die() { printf "\033[38;5;203m✕\033[0m %s\n" "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "Ameka Voice is macOS only right now."

PY=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
  [ -x "$candidate" ] || continue
  if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)'; then
    PY="$candidate"; break
  fi
done
[ -n "$PY" ] || die "Python 3.11+ required. Install it with:  brew install python"

say "Installing Ameka Voice $VERSION"
rm -rf "$APP_DIR"; mkdir -p "$APP_DIR" "$BIN_DIR"

if [ -n "${AMEKA_LOCAL_SRC:-}" ]; then
  cp -R "$AMEKA_LOCAL_SRC"/. "$APP_DIR"/
else
  TARBALL="$(mktemp -t ameka).tar.gz"
  curl -fsSL "$BASE/downloads/ameka-voice-macos.tar.gz" -o "$TARBALL" \
    || die "Download failed from $BASE/downloads/ameka-voice-macos.tar.gz"
  tar -xzf "$TARBALL" -C "$APP_DIR" --strip-components=1
  rm -f "$TARBALL"
fi

say "Setting up a private Python environment"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet sounddevice numpy websocket-client || \
  say "Audio libraries failed to install — falling back to built-in macOS voice."

cat > "$BIN_DIR/ameka" <<LAUNCHER
#!/bin/bash
exec "$VENV/bin/python" -m amekavoice "\$@"
LAUNCHER
chmod +x "$BIN_DIR/ameka"

cat > "$VENV/lib/$("$VENV/bin/python" -c 'import sys;print(f"python{sys.version_info.major}.{sys.version_info.minor}")')/site-packages/ameka.pth" <<PTH
$APP_DIR
PTH

if [ -z "${AMEKA_NO_MODELS:-}" ]; then
  say "Installing the on-device voice and hearing engines (about 500 MB, one time)"
  "$VENV/bin/python" -m amekavoice setup-local || say "Local engines incomplete — ameka doctor will say what is missing."
fi

if [ -d "$APP_DIR/native/Ameka.app" ]; then
  say "Installing the Ameka menu bar app"
  rm -rf /Applications/Ameka.app
  cp -R "$APP_DIR/native/Ameka.app" /Applications/
  codesign --force --deep --sign - /Applications/Ameka.app >/dev/null 2>&1 || true
  cat > "$HOME/Library/LaunchAgents/ai.ameka.menu.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.ameka.menu</string>
  <key>ProgramArguments</key><array><string>/Applications/Ameka.app/Contents/MacOS/Ameka</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
PLIST
  launchctl unload "$HOME/Library/LaunchAgents/ai.ameka.menu.plist" 2>/dev/null || true
  launchctl load "$HOME/Library/LaunchAgents/ai.ameka.menu.plist" 2>/dev/null || true
  open -a /Applications/Ameka.app 2>/dev/null || true
fi

say "Giving her a name macOS will use when it asks you for permission"
"$VENV/bin/python" -c "import sys; sys.path.insert(0, '$APP_DIR'); from amekavoice import appbundle, __version__; print('  ', appbundle.build(__version__) or 'not built — permission sheets will say python')"

say "Wiring Claude Code hooks, and starting Ameka at login"
# The login agent is what keeps her running: it brings her back after a
# self-update, after a hung audio device, after a reboot. Without it she is a
# terminal window someone will eventually close.
"$BIN_DIR/ameka" install --login-agent || die "Hook install failed."

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "Add this to your shell profile:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

cat <<EOF

  Installed, and running. Ameka starts at login from now on and keeps herself
  up to date from ameka.ai.

    Check everything     ameka doctor
    Quiet her            ameka mute on        (ameka mute off to resume)
    See what she hears   ameka status

  macOS will ask once for the Microphone and once for Accessibility — the
  second is how she types your answer back into a session.

  No API key needed. If you would rather use OpenAI's voice: ameka key openai

  Then finish a Claude Code session with your AirPods in.

EOF
