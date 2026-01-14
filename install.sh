#!/bin/bash

echo "Camera Grid Viewer - Installing dependencies..."

# Create requirements.txt
cat > requirements.txt << 'EOF'
PyQt6
opencv-python
qdarkstyle
imutils
cv2-enumerate-cameras
EOF

echo "Installing Python packages..."
pip install -r requirements.txt

echo "Fixing camera permissions (Linux)..."
if ! groups | grep -q video; then
    echo "Add to video group: sudo usermod -a -G video $USER"
    echo "(logout/login required)"
fi

chmod +x camera_grid.py

echo ""
echo "Setup complete!"
echo "Run: ./camera_grid.py or python camera_grid.py"
echo "Quit: Ctrl+Q"
