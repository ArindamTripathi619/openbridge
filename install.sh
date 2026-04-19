#!/bin/bash
set -e

echo "🤖 TeleWatch Installation Script"
echo "==================================="
echo ""

# Check Python version
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Please install Python 3.8 or later."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "✓ Found Python $PYTHON_VERSION"

# Determine installation directory
if [ "$EUID" -eq 0 ]; then
    # Running as root, install system-wide
    INSTALL_DIR="/opt/telewatch"
    BIN_DIR="/usr/local/bin"
    echo "📦 Installing system-wide to $INSTALL_DIR"
else
    # User installation
    INSTALL_DIR="$HOME/.local/telewatch"
    BIN_DIR="$HOME/.local/bin"
    echo "📦 Installing to $INSTALL_DIR (user-local)"
fi

# Create installation directory
mkdir -p "$INSTALL_DIR"
mkdir -p "$BIN_DIR"

# Create virtual environment
echo ""
echo "Creating virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"

# Activate virtual environment
source "$INSTALL_DIR/venv/bin/activate"

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip > /dev/null

# Install package
echo "Installing telewatch..."
if [ -f "setup.py" ]; then
    # Installing from source
    pip install -e . > /dev/null
else
    echo "❌ setup.py not found. Please run this script from the repository root."
    exit 1
fi

# Create symlink
echo "Creating executable symlink..."
ln -sf "$INSTALL_DIR/venv/bin/telewatch" "$BIN_DIR/telewatch"

# Check if bin directory is in PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    echo "⚠️  $BIN_DIR is not in your PATH"
    echo "   Add this to your ~/.bashrc or ~/.zshrc:"
    echo "   export PATH=\"$BIN_DIR:\$PATH\""
fi

# Optional Systemd Integration
if [ -d "/etc/systemd/system" ] && [ "$EUID" -eq 0 ]; then
    echo ""
    read -p "❓ Do you want to generate a systemd service file? (y/N) " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        SERVICE_FILE="/etc/systemd/system/telewatch.service"
        echo "Creating $SERVICE_FILE..."
        cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=TeleWatch Remote Process Monitor
After=network.target

[Service]
Type=forking
User=$SUDO_USER
ExecStart=$BIN_DIR/telewatch start -d
ExecStop=$BIN_DIR/telewatch stop
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        echo "✅ Systemd service created and reloaded."
        echo "   To start: systemctl start telewatch"
        echo "   To enable on boot: systemctl enable telewatch"
    fi
fi

echo ""
echo "✓ Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Run: telewatch setup"
echo "  2. Edit config: ~/.telewatch/config.yaml"
echo "  3. Start monitoring: telewatch start"
echo ""
echo "For help: telewatch --help"
