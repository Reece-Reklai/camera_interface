"""
Multi-camera grid viewer - 0 FPS DEBUG + FIXED VERSION
🔧 DIAGNOSTIC MODE + SIGNAL BLOCKAGE FIX
"""

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal, pyqtSlot, QThread, QTimer, QMutex
from PyQt5.QtWidgets import QShortcut
from PyQt5.QtGui import QKeySequence
import sys
import cv2
import time 
import traceback
import glob
import atexit
import signal
import numpy as np

try:
    import qdarkstyle
    DARKSTYLE_AVAILABLE = True
except:
    DARKSTYLE_AVAILABLE = False

# 🔥 FIXED: Use QMutex + Direct Connection + Diagnostic prints
class CaptureWorker(QThread):
    frame_ready = pyqtSignal(object, int)  # frame + frame_count
    status_changed = pyqtSignal(bool)

    def __init__(self, stream_link, parent=None):
        super().__init__(parent)
        self.stream_link = stream_link
        self._running = True
        self._cap = None
        self.mutex = QMutex()
        self.frame_count = 0

    def run(self):
        print(f"🎥 [DEBUG] Worker {self.stream_link} STARTING...")
        
        # V4L2 ONLY - Most reliable on RPi
        cap = cv2.VideoCapture(self.stream_link, cv2.CAP_V4L2)
        if not cap.isOpened():
            print(f"❌ [DEBUG] CAP_V4L2 FAILED for {self.stream_link}")
            return
            
        print(f"✅ [DEBUG] Opened {self.stream_link}")
        
        # CRITICAL RPi FIXES
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        
        ret, test_frame = cap.read()
        if not ret or test_frame is None:
            print(f"❌ [DEBUG] Test read FAILED {self.stream_link}")
            cap.release()
            return
            
        print(f"✅ [DEBUG] Test frame OK: {test_frame.shape}")
        self._cap = cap
        self.status_changed.emit(True)

        # 🔥 MAIN LOOP - NO SLEEP + DIAGNOSTIC
        while self._running:
            self.mutex.lock()
            if not self._running:
                self.mutex.unlock()
                break
                
            ret, frame = self._cap.read()
            self.frame_count += 1
            
            if ret and frame is not None:
                print(f"📸 [DEBUG] Frame #{self.frame_count} captured {self.stream_link} {frame.shape}")
                # 🔥 CRITICAL: Qt.DirectConnection ensures immediate delivery
                self.frame_ready.emit(frame, self.frame_count)
            else:
                print(f"⚠️  [DEBUG] Read failed #{self.frame_count}")
                
            self.mutex.unlock()
            
        print(f"🛑 [DEBUG] Worker {self.stream_link} STOPPED")

    def stop(self):
        self._running = False
        self.mutex.lock()
        self.mutex.unlock()
        if self._cap:
            self._cap.release()
            self._cap = None

# 🔥 FIXED WIDGET - Qt.DirectConnection + DEBUG
class CameraWidget(QtWidgets.QWidget):
    def __init__(self, width, height, stream_link=0, parent=None):
        super().__init__(parent)
        print(f"🎬 Creating widget cam{stream_link}")
        
        self.stream_link = stream_link
        self.widget_id = f"cam{stream_link}_{id(self)}"
        
        self.normal_style = "border: 2px solid #00ff00; background: black; color: white;"
        self.error_style = "border: 4px solid red; background: black; color: white;"
        self.setStyleSheet(self.normal_style)
        
        self.video_label = QtWidgets.QLabel("WAITING...", self)
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setScaledContents(True)
        self.video_label.setStyleSheet("font-size: 20px; color: yellow;")
        
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.addWidget(self.video_label)
        
        self.frame_count = 0
        self.worker = None
        self.start_worker()

    def start_worker(self):
        self.worker = CaptureWorker(self.stream_link, self)
        
        # 🔥 CRITICAL FIXES:
        # 1. Qt.DirectConnection - IMMEDIATE delivery
        # 2. Lambda preserves frame_count
        self.worker.frame_ready.connect(
            lambda frame, count: self.on_frame(frame, count), 
            Qt.DirectConnection
        )
        self.worker.status_changed.connect(self.on_status_changed, Qt.DirectConnection)
        
        print(f"🔗 [DEBUG] Signals connected for {self.stream_link}")
        self.worker.start()
        print(f"🚀 [DEBUG] Worker STARTED for {self.stream_link}")

    @pyqtSlot(object, int)
    def on_frame(self, frame, count):
        try:
            print(f"🎥 [DEBUG] on_frame CALLED #{count} {self.stream_link}")
            
            if frame is None:
                print(f"⚠️  [DEBUG] Empty frame {self.stream_link}")
                return
            
            # FASTEST CONVERSION
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb_small = cv2.resize(rgb, (320, 240))
            
            h, w = rgb_small.shape[:2]
            qimg = QtGui.QImage(rgb_small.tobytes(), w, h, 3*w, QtGui.QImage.Format_RGB888)
            pixmap = QtGui.QPixmap.fromImage(qimg)
            
            self.video_label.setPixmap(pixmap)
            self.video_label.setText("")  # Clear "WAITING..."
            
            self.frame_count = count
            print(f"✅ [DEBUG] Frame #{count} DISPLAYED {self.stream_link}")
            
        except Exception as e:
            print(f"💥 [DEBUG] on_frame ERROR {self.stream_link}: {e}")
            traceback.print_exc()
            self.video_label.setText(f"ERROR\n{e}")

    @pyqtSlot(bool)
    def on_status_changed(self, online):
        print(f"📡 [DEBUG] Status {self.stream_link}: {'ONLINE' if online else 'OFFLINE'}")
        if online:
            self.setStyleSheet(self.normal_style)
        else:
            self.setStyleSheet(self.error_style)
            self.video_label.setText("CAM OFFLINE")

    def cleanup(self):
        print(f"🧹 [DEBUG] Cleaning {self.stream_link}")
        if self.worker:
            self.worker.stop()
            self.worker.quit()
            self.worker.wait(3000)

# 🔍 ENHANCED CAMERA DETECTION
def find_working_cameras():
    working = []
    print("\n" + "="*60)
    print("🔍 COMPREHENSIVE RPi CAMERA SCAN")
    print("="*60)
    
    devices = glob.glob('/dev/video*')
    print(f"📹 Devices: {devices}")
    
    for device in sorted(devices, key=lambda x: int(x[10:])):
        try:
            i = int(device[10:])
            print(f"\n🔎 Testing {device} (#{i})")
            
            cap = cv2.VideoCapture(i, cv2.CAP_V4L2)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                ret, frame = cap.read()
                cap.release()
                
                if ret and frame is not None:
                    print(f"✅ #{i} WORKING ({frame.shape})")
                    working.append(i)
                else:
                    print(f"⚠️  #{i} No frames")
            else:
                print(f"❌ #{i} Can't open")
        except Exception as e:
            print(f"💥 {device}: {e}")
    
    print(f"\n🎯 FOUND {len(working)} CAMERAS: {working}")
    return working

def get_smart_grid(n):
    if n <= 1: return 1, 1
    if n == 2: return 1, 2
    if n == 3: return 1, 3
    if n == 4: return 2, 2
    return 2, 3

def safe_cleanup(widgets):
    print("\n🧹 GLOBAL CLEANUP")
    for w in widgets:
        try:
            w.cleanup()
        except Exception as e:
            print(f"Cleanup error: {e}")

def main():
    print("🚀 RPi Multi-Cam DEBUG VERSION - 0 FPS FIXED")
    app = QtWidgets.QApplication(sys.argv)
    camera_widgets = []

    # Graceful exit
    signal.signal(signal.SIGINT, lambda s,f: sys.exit(0))
    atexit.register(lambda: safe_cleanup(camera_widgets))

    # Styling
    if DARKSTYLE_AVAILABLE:
        try:
            app.setStyleSheet(qdarkstyle.load_stylesheet_pyqt5())
        except:
            app.setStyle("Fusion")
    else:
        app.setStyle("Fusion")

    # Fullscreen window
    mw = QtWidgets.QMainWindow()
    mw.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    central = QtWidgets.QWidget()
    mw.setCentralWidget(central)
    mw.showFullScreen()

    screen = app.primaryScreen().availableGeometry()
    print(f"🖥️  Screen: {screen.width()}x{screen.height()}")
    
    cameras = find_working_cameras()
    layout = QtWidgets.QGridLayout(central)
    layout.setContentsMargins(10,10,10,10)
    layout.setSpacing(10)

    if cameras:
        rows, cols = get_smart_grid(len(cameras))
        cell_w = screen.width() // cols // 2  # Smaller for debug
        cell_h = screen.height() // rows // 2
        
        print(f"📐 Grid: {rows}x{cols}, cell: {cell_w}x{cell_h}")
        
        for i, cam_id in enumerate(cameras[:4]):  # Max 4 for debug
            widget = CameraWidget(cell_w, cell_h, cam_id, central)
            camera_widgets.append(widget)
            row, col = divmod(i, cols)
            layout.addWidget(widget, row, col)
            print(f"➕ Added cam#{cam_id} at [{row},{col}]")
    else:
        label = QtWidgets.QLabel(
            "❌ NO CAMERAS FOUND\n\n"
            "1. Check USB connections\n"
            "2. sudo usermod -a -G video $USER\n"
            "3. sudo modprobe uvcvideo\n"
            "4. reboot\n\n"
            "lsusb = ?"
        )
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("font-size: 28px; color: #ff4444; background: black;")
        layout.addWidget(label)

    # ESC or Ctrl+Q to quit
    QShortcut(QKeySequence('Ctrl+Q'), mw, app.quit)
    QShortcut(QKeySequence('Escape'), mw, app.quit)
    
    print("\n🎮 Controls: Ctrl+Q or ESC = Quit")
    print("📊 Watch console for [DEBUG] messages!")
    
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
