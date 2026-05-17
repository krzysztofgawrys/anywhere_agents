#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$ROOT_DIR/backend"

SERVICE_NAME="claude-web"

install_systemd() {
    local unit_dir="$HOME/.config/systemd/user"
    mkdir -p "$unit_dir"

    cat > "$unit_dir/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Claude Web Backend
After=network.target

[Service]
Type=simple
WorkingDirectory=$BACKEND_DIR
ExecStart=/bin/bash -lc "exec uv run uvicorn src.main:app --host 127.0.0.1 --port 8001"
Restart=on-failure
RestartSec=5
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user start "$SERVICE_NAME"
    echo "Installed and started systemd user unit: $SERVICE_NAME"
    echo "Check status: systemctl --user status $SERVICE_NAME"
}

install_launchd() {
    local plist_dir="$HOME/Library/LaunchAgents"
    local plist_path="$plist_dir/com.claude-web.backend.plist"
    mkdir -p "$plist_dir"

    cat > "$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude-web.backend</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-lc</string>
        <string>cd $BACKEND_DIR && exec uv run uvicorn src.main:app --host 127.0.0.1 --port 8001</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/claude-web.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude-web.stderr.log</string>
</dict>
</plist>
EOF

    launchctl load "$plist_path"
    echo "Installed and loaded launchd agent: com.claude-web.backend"
    echo "Check: launchctl list | grep claude-web"
}

# Detect OS
if [[ "$(uname)" == "Darwin" ]]; then
    install_launchd
elif [[ "$(uname)" == "Linux" ]]; then
    install_systemd
else
    echo "Unsupported OS: $(uname)"
    exit 1
fi
