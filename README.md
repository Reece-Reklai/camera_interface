Camera Grid Viewer

Fullscreen multi-camera display with click-to-fullscreen and hold-to-swap.
Quick Start

bash
# 1. Install
chmod +x install.sh
./install.sh

# 2. Run
./camera_grid.py

Controls

    Click any camera = toggle fullscreen

    Hold 400ms = yellow border (swap mode)

    Click other camera = swap positions

    Click yellow camera = clear swap mode

    Ctrl+Q = quit

Install (manual)

bash
pip install PyQt6 opencv-python qdarkstyle imutils cv2-enumerate-cameras
chmod +x camera_grid.py

Linux only: sudo usermod -a -G video $USER (then logout/login)
Files

text
├── camera_grid.py     # Main app
├── install.sh         # Setup script
├── requirements.txt   # pip install -r
└── README.md          # This file

Debug

Terminal shows all activity:

text
DEBUG: Found 2 cameras: [0, 1]
DEBUG: Press cam0_140123456
DEBUG: ENTER swap cam0_140123456  
DEBUG: SWAP cam0_140123456 ↔ cam1_140123789

Troubleshooting
Issue	Solution
No cameras	ls /dev/video* - plug in USB cameras
Permissions	sudo usermod -a -G video $USER
Import error	pip install -r requirements.txt
Won't quit	Ctrl+Q or Ctrl+C
Requirements

text
PyQt6
opencv-python  
qdarkstyle
imutils
cv2-enumerate-cameras

Tested

    Arch linux via AMD CPU and Nvidia GPU

    Raspberry Pi 4 and 5 via Intel5 CPU

