#!/usr/bin/env bash
set -euo pipefail

# Camera Dashboard installer for Raspberry Pi / Linux
# Run this script from the project root directory:
#   chmod +x install.sh
#   ./install.sh

# ---------- helper functions ----------

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

echo_section() {
  echo
  echo "========================================"
  echo "$1"
  echo "========================================"
}

# ---------- 0) basic checks ----------

if [[ "$EUID" -eq 0 ]]; then
  echo "Do NOT run this script as root or with sudo."
  echo "Use a normal user and only enter your password for sudo when prompted."
  exit 1
fi

if ! command_exists sudo; then
  echo "sudo is required but not found. Please install/configure sudo and try again."
  exit 1
fi

if ! command_exists python3; then
  echo "python3 is required but not found. Please install Python 3 and try again."
  exit 1
fi
python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("Python 3.9+ is required")
PY

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------- 1) update the system ----------

echo_section "1) Updating system packages (sudo apt update && sudo apt upgrade -y)"
sudo apt update
sudo apt upgrade -y

# ---------- 2) install system dependencies ----------

echo_section "2) Installing system dependencies (sudo apt install ...)"

# Core Python and Qt6 dependencies
sudo apt install -y \
  python3 python3-pip python3-venv \
  python3-pyqt6 python3-opencv python3-numpy \
  libgl1 libegl1 libxkbcommon0 libxkbcommon-x11-0 \
  libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
  libxcb-render-util0 libxcb-xinerama0 libxcb-xfixes0 \
  libqt6gui6 libqt6widgets6 \
  v4l-utils

# GStreamer for hardware-accelerated video capture (jpegdec pipeline)
sudo apt install -y gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad || true

# ---------- 3) create virtual environment with system packages ----------

echo_section "3) Creating Python virtual environment (.venv with system-site-packages)"

# Remove old venv if it exists without system packages
if [[ -d ".venv" ]]; then
  if ! grep -q "include-system-site-packages = true" ".venv/pyvenv.cfg" 2>/dev/null; then
    echo "Removing old venv (missing system-site-packages)..."
    rm -rf .venv
  fi
fi

if [[ ! -d ".venv" ]]; then
  python3 -m venv --system-site-packages .venv
else
  echo ".venv already exists with system packages, reusing it."
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# ---------- 4) verify python packages ----------

echo_section "4) Verifying Python packages"

pip install --upgrade pip

# Install test dependencies
pip install --quiet pytest pytest-qt

# Test imports
if python3 -c "from PyQt6 import QtCore, QtGui, QtWidgets; import cv2; import pytest; print('All imports OK')" 2>/dev/null; then
  echo "All required Python packages are available."
else
  echo "ERROR: Required Python packages not available!"
  echo "Please ensure python3-pyqt6 and python3-opencv are installed."
  exit 1
fi

# ---------- 5) fix camera permissions ----------

echo_section "5) Adding current user to 'video' group for camera access"

if ! groups "$USER" | grep -q "\bvideo\b"; then
  sudo usermod -aG video "$USER"
  echo "Added $USER to video group."
  echo "You may need to log out and back in (or reboot) for group changes to take effect."
else
  echo "$USER is already in the video group."
fi

echo "Camera devices:"
ls -l /dev/video* 2>/dev/null | head -10 || echo "No video devices found"

# ---------- 6) create logs directory ----------

echo_section "6) Creating logs directory"

mkdir -p "$SCRIPT_DIR/logs"
echo "Logs directory: $SCRIPT_DIR/logs"

# ---------- 7) create desktop shortcut ----------

echo_section "7) Creating desktop shortcut"

DESKTOP_DIR="$HOME/Desktop"
DESKTOP_FILE="$DESKTOP_DIR/CameraDashboard.desktop"

mkdir -p "$DESKTOP_DIR"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Name=Camera Dashboard
Comment=Multi-camera monitoring dashboard
Exec=$SCRIPT_DIR/.venv/bin/python3 $SCRIPT_DIR/main.py
Path=$SCRIPT_DIR
Icon=camera-video
Terminal=false
Type=Application
Categories=Video;Monitor;
StartupNotify=true
EOF

chmod +x "$DESKTOP_FILE"
echo "Desktop shortcut created: $DESKTOP_FILE"
echo "Double-click the icon on your desktop to launch the app."

# ---------- 8) quick test ----------

echo_section "8) Quick test"

if timeout 5 python3 -c "
from PyQt6 import QtWidgets
import cv2
import sys

# Test OpenCV can access a camera
cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
if cap.isOpened():
    ret, frame = cap.read()
    cap.release()
    if ret:
        print('Camera test: OK (captured frame)')
    else:
        print('Camera test: WARNING (opened but no frame)')
else:
    print('Camera test: WARNING (could not open camera 0)')

print('Qt test: OK')
" 2>/dev/null; then
  echo "Quick test passed!"
else
  echo "Quick test had issues (this may be normal if no display is available)"
fi

echo_section "9) Finished"

cat <<EOF
Installation complete!

To run the app:

  Double-click "Camera Dashboard" on your desktop
  
  Or from terminal:
    cd $SCRIPT_DIR
    source .venv/bin/activate
    python3 main.py

To run tests:

  ./test.sh           # Run all tests
  ./test.sh -v        # Verbose output
  ./test.sh -k "config"  # Run specific tests

To view logs:

  tail -f logs/camera_dashboard.log

Controls:
- Click on camera: Toggle fullscreen view
- Press Q: Quit application
- Hold click 400ms: Enter swap mode

EOF
