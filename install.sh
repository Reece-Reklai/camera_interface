#!/usr/bin/env bash
set -euo pipefail # basically fails at first error instead of running to finish script

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

# ---------- 0) basic checks ----------

if [[ "$EUID" -eq 0 ]]; then
  echo "Do NOT run this script as root or with sudo."
  echo "Use a normal user and only enter your password for sudo when prompted."
  exit 1
fi

if ! command_exists sudo; then
  echo "sudo is required but not found."
  exit 1
fi

if ! command_exists python3; then
  echo "python3 is required but not found."
  exit 1
fi

# ---------- 1) update the system ----------

echo "Updating system packages (sudo apt update && sudo apt upgrade -y)"
sudo apt update
sudo apt upgrade -y

# ---------- 2) install system dependencies ----------

echo "Installing system dependencies (sudo apt install ...)"

sudo apt install -y \
  python3 python3-pip python3-venv \
  libgl1 libegl1 libxkbcommon0 libxkbcommon-x11-0 \
  libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
  libxcb-render-util0 libxcb-xinerama0 libxcb-xfixes0 \
  libqt6gui6 libqt6widgets6

# ---------- 3) create and activate virtual environment ----------

echo_section "3) Creating Python virtual environment (.venv)"

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
else
  echo ".venv already exists, reusing it."
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# ---------- 4) install python packages ----------

pip install --upgrade pip
if ! pip install PyQt6 opencv-python pyudev; then
  echo
  echo "pip installation failed for PyQt6 / opencv-python / pyudev."
  echo "Falling back to system packages via apt (sudo)."
  echo

  deactivate || true

  sudo apt install -y python3-opencv
  sudo apt install -y python3-pyqt6
  sudo apt install -y python3-pyudev
  sudo apt install -y python3-pyqt6 python3-opencv python3-opengl
  sudo apt install -y gstreamer1.0-tools gstreamer1.0-plugins-good
  sudo apt install -y python3-picamera2

  # Reactivate venv if it exists (even if system Python packages are used)
  # shellcheck disable=SC1091
  source .venv/bin/activate || true
fi

# ---------- 5) fix camera permissions ----------

sudo usermod -aG video "$USER"

echo
echo "You may need to log out and back in (or reboot) for group changes to take effect."
echo "You can later check permissions with:  ls -l /dev/video*"

