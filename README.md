Camera Grid Viewer

Fullscreen multi-camera display - click for fullscreen, hold-to-swap cameras in grid.
Quick Start
1. Install Dependencies

bash
pip install PyQt6 opencv-python qdarkstyle imutils cv2-enumerate-cameras

2. Plug in USB Cameras

    Works with /dev/video0, /dev/video1, etc

    Webcams, USB cameras, industrial cameras all work

    No config needed - auto-detects

3. Run

bash
python camera_grid.py

App opens fullscreen with auto-detected cameras in smart grid layout.
Controls
Action	How	Result
Single click	Click any camera	Toggle fullscreen
Hold 400ms	Long-press camera	Yellow border = swap mode
Swap cameras	Click other camera	Instant position swap
Clear swap	Click yellow camera	Back to normal
Quit	Ctrl+Q	Clean shutdown
Example Usage

text
2 cameras detected → 1×2 grid
Click cam0 → Fullscreen cam0  
Click again → Back to grid
Hold cam1 400ms → Yellow border on cam1
Click cam0 → cam0 & cam1 swap positions
Click yellow cam1 → Normal mode

Debug Output

Watch terminal for real-time status:

text
DEBUG: Found 2 cameras: [0, 1]
DEBUG: Grid 1×2, widget 1920x1080
DEBUG: Press cam0_140123456
DEBUG: Release cam0_140123456, hold=380ms
DEBUG: Short click fullscreen cam0_140123456

Troubleshooting
Problem	Fix
No cameras found	Plug in USB camera
ImportError	pip install PyQt6 opencv-python
Permission denied	sudo usermod -a -G video $USER (Linux)
GUI freezes	Already fixed - threaded capture
Won't quit	Ctrl+Q or Ctrl+C
System Requirements

Linux (Arch linux and Raspbian tested)

    Python 3.8+

    USB cameras (/dev/videoX)

    X11 display (Wayland may need tweaks)

File Structure

text
├── main.py     # Main app
├── README.md   # This file
└── install.sh  # Install esential packages

Tested: Raspberry Pi 4/5/OS and Arch linux