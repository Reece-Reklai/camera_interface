"""
Multi-camera grid viewer
Enhanced with FPS-based hotplug detection for Raspberry Pi
"""

from PyQt6 import QtCore, QtGui, QtWidgets  # GUI framework - makes windows, buttons
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QThread, QTimer  # Core Qt features
import sys  # System utilities - exit cleanly
import cv2  # OpenCV - reads camera video frames
import time 
import traceback  # Error reporting - show crashes clearly
from collections import deque  # Ring buffer - keeps last 4 frames
from cv2_enumerate_cameras import enumerate_cameras  # Finds all /dev/video devices
import qdarkstyle  
import imutils  # Image resizing utilities
import atexit  # Runs cleanup code when program exits
import signal  # Handles Ctrl+C gracefully

# 🔥 ENHANCED CAMERA THREAD = Reads video + FPS monitoring + auto-reconnect
class CaptureWorker(QThread):
    """
    Runs in background thread. Reads camera frames every 10ms.
    Emits new frames to main thread via signals (thread-safe).
    Auto-reconnects if camera disconnects OR 0 FPS detected.
    """
    frame_ready = pyqtSignal(object)  # Sends frame to GUI
    status_changed = pyqtSignal(bool)  # Online/offline status

    def __init__(self, stream_link, parent=None, maxlen=4):
        super().__init__(parent)
        self.stream_link = stream_link  # Camera ID (0, 1, 2...)
        self._running = True  # Stop flag
        self._reconnect_backoff = 1.0  # Wait longer each reconnect attempt
        self._cap = None  # OpenCV camera object
        self.buffer = deque(maxlen=maxlen)  # Keeps last 4 frames
        
        # 🔥 FPS MONITORING VARIABLES
        self.frame_count = 0
        self.fps_check_start = time.time()
        self.zero_fps_count = 0
        self.zero_fps_threshold = 3.0  # Trigger reconnect after 3s of 0 FPS
        self.last_good_frame_time = time.time()

    def run(self):
        """Infinite loop - grab frames until stopped."""
        print(f"DEBUG: Camera {self.stream_link} thread started")
        while self._running:
            try:
                # 🔥 CHECK FOR RECONNECT: Disconnected OR 0 FPS
                if self._needs_reconnect():
                    self._trigger_reconnect()
                    continue

                # Read one frame
                status, frame = self._cap.read()
                if not status or frame is None:
                    self._close_capture()
                    self.status_changed.emit(False)
                    continue

                # 🔥 FRAME SUCCESS: Update FPS monitoring
                self._update_fps_monitor(frame)
                self.last_good_frame_time = time.time()  # Reset timer
                
                # Send frame to GUI thread
                self.buffer.append(frame)
                self.frame_ready.emit(frame)
                time.sleep(0.01)  # Don't overload CPU (Raspberry Pi friendly)
                
            except Exception:
                traceback.print_exc()
                time.sleep(0.5)
        
        self._close_capture()

    def _needs_reconnect(self):
        """🔥 Check if camera disconnected OR stalled (0 FPS too long)"""
        # Case 1: Camera not open
        if self._cap is None or not self._cap.isOpened():
            return True
        
        # Case 2: 🔥 No good frames for 3+ seconds (0 FPS detection)
        if (time.time() - self.last_good_frame_time) > self.zero_fps_threshold:
            print(f"DEBUG: Camera {self.stream_link} 0 FPS for {time.time() - self.last_good_frame_time:.1f}s → forcing reconnect")
            return True
            
        return False

    def _trigger_reconnect(self):
        """🔥 Handle reconnect (both normal + FPS trigger)"""
        self._close_capture()
        
        if not self._open_capture() or not (self._cap and self._cap.isOpened()):
            # Failed - exponential backoff
            time.sleep(self._reconnect_backoff)
            self._reconnect_backoff = min(self._reconnect_backoff * 1.5, 10.0)
            self.status_changed.emit(False)
            return False
        
        self.status_changed.emit(True)
        self._reconnect_backoff = 1.0  # Reset backoff on success
        print(f"DEBUG: Camera {self.stream_link} reconnected")
        return True

    def _update_fps_monitor(self, frame):
        """🔥 Track FPS and detect stalls"""
        self.frame_count += 1
        now = time.time()
        
        # Calculate FPS every second
        if now - self.fps_check_start >= 1.0:
            fps = self.frame_count / (now - self.fps_check_start)
            self.frame_count = 0
            self.fps_check_start = now
            
            if fps < 0.5:  # Effectively stalled
                self.zero_fps_count += 1
                print(f"DEBUG: Camera {self.stream_link} LOW FPS: {fps:.1f} (count={self.zero_fps_count})")
            else:
                self.zero_fps_count = 0

    def _open_capture(self):
        """Try V4L2 first (Linux USB cameras), then any backend."""
        try:
            for api in [cv2.CAP_V4L2, cv2.CAP_ANY]:
                cap = cv2.VideoCapture(self.stream_link, api)
                if cap.isOpened():
                    # MJPG = lower CPU usage
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
                    self._cap = cap
                    return True
                cap.release()
        except:
            pass
        return False

    def _close_capture(self):
        """Safely close camera."""
        try:
            if self._cap:
                self._cap.release()
                self._cap = None
        except:
            pass

    def stop(self):
        """Stop thread cleanly."""
        self._running = False
        self.wait(timeout=2000)
        self._close_capture()

# CAMERA WIDGET = Each camera lives here (UNCHANGED)
class CameraWidget(QtWidgets.QWidget):
    hold_threshold_ms = 400

    def __init__(self, width, height, stream_link=0, aspect_ratio=False, parent=None, buffer_size=4):
        super().__init__(parent)
        print(f"DEBUG: Creating camera {stream_link}")
        
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)
        
        self.screen_width = max(1, width)
        self.screen_height = max(1, height)
        self.maintain_aspect_ratio = aspect_ratio
        self.camera_stream_link = stream_link
        
        self.widget_id = f"cam{stream_link}_{id(self)}"
        self.is_fullscreen = False
        self.grid_position = None
        self._saved_parent = None
        self._saved_position = None
        self._press_widget_id = None
        self._press_time = 0
        self._grid_parent = None

        self.normal_style = "border: 2px solid #555; background: black;"
        self.swap_ready_style = "border: 4px solid #FFFF00; background: black;"
        self.setStyleSheet(self.normal_style)
        self.setObjectName(self.widget_id)

        self.video_label = QtWidgets.QLabel(self)
        self.video_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                       QtWidgets.QSizePolicy.Policy.Expanding)
        self.video_label.setMinimumSize(1, 1)
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setScaledContents(True)
        self.video_label.setMouseTracking(True)
        self.video_label.setObjectName(f"{self.widget_id}_label")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.video_label)

        self.frame_count = 0
        self.prev_time = time.time()

        self.worker = CaptureWorker(stream_link, parent=self, maxlen=buffer_size)
        self.worker.frame_ready.connect(self.on_frame)
        self.worker.status_changed.connect(self.on_status_changed)
        self.worker.start()

        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(1000)
        self.ui_timer.timeout.connect(self._print_fps)
        self.ui_timer.start()

        self.installEventFilter(self)
        self.video_label.installEventFilter(self)
        print(f"DEBUG: Widget {self.widget_id} ready")

    def eventFilter(self, obj, event):
        if obj not in (self, self.video_label):
            return super().eventFilter(obj, event)
            
        if event.type() == QtCore.QEvent.Type.MouseButtonPress:
            return self._on_mouse_press(event)
        if event.type() == QtCore.QEvent.Type.MouseButtonRelease:
            return self._on_mouse_release(event)
        return super().eventFilter(obj, event)

    def _on_mouse_press(self, event):
        try:
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                self._press_time = time.time() * 1000.0
                self._press_widget_id = self.widget_id
                self._grid_parent = self.parent()
                print(f"DEBUG: Press {self.widget_id}")
            elif event.button() == QtCore.Qt.MouseButton.RightButton:
                self.toggle_fullscreen()
        except Exception:
            traceback.print_exc()
        return True

    def _on_mouse_release(self, event):
        try:
            if (event.button() != QtCore.Qt.MouseButton.LeftButton or 
                not self._press_widget_id or self._press_widget_id != self.widget_id):
                return True

            hold_time = (time.time() * 1000.0) - self._press_time
            print(f"DEBUG: Release {self.widget_id}, hold={hold_time:.0f}ms")

            swap_parent = self._grid_parent
            if not swap_parent or not hasattr(swap_parent, 'selected_camera'):
                self._reset_mouse_state()
                self.toggle_fullscreen()
                return True

            if swap_parent.selected_camera == self:
                print(f"DEBUG: Clear swap {self.widget_id}")
                swap_parent.selected_camera = None
                self.reset_style()
                self._reset_mouse_state()
                return True

            if (swap_parent.selected_camera and 
                swap_parent.selected_camera != self and 
                not self.is_fullscreen):
                other = swap_parent.selected_camera
                print(f"DEBUG: SWAP {other.widget_id} ↔ {self.widget_id}")
                self.do_swap(other, self, swap_parent)
                other.reset_style()
                swap_parent.selected_camera = None
                self._reset_mouse_state()
                return True

            if hold_time >= self.hold_threshold_ms and not self.is_fullscreen:
                print(f"DEBUG: ENTER swap {self.widget_id}")
                swap_parent.selected_camera = self
                self.video_label.setStyleSheet(self.swap_ready_style)
                self._reset_mouse_state()
                return True

            print(f"DEBUG: Short click fullscreen {self.widget_id}")
            self.toggle_fullscreen()
            
        except Exception:
            traceback.print_exc()
        finally:
            self._reset_mouse_state()
        return True

    def _reset_mouse_state(self):
        self._press_time = 0
        self._press_widget_id = None
        self._grid_parent = None

    def do_swap(self, source, target, layout_parent):
        try:
            source_pos = getattr(source, 'grid_position', None)
            target_pos = getattr(target, 'grid_position', None)
            if source_pos is None or target_pos is None:
                print(f"DEBUG: Swap failed - missing positions")
                return

            layout = layout_parent.layout()
            layout.removeWidget(source)
            layout.removeWidget(target)
            layout.addWidget(target, *source_pos)
            layout.addWidget(source, *target_pos)
            source.grid_position, target.grid_position = target_pos, source_pos
            print(f"DEBUG: Swap complete {source.widget_id} ↔ {target.widget_id}")
        except Exception:
            traceback.print_exc()

    def toggle_fullscreen(self):
        if self.is_fullscreen:
            self.exit_fullscreen()
        else:
            self.go_fullscreen()

    def go_fullscreen(self):
        if self.is_fullscreen:
            return
        try:
            print(f"DEBUG: {self.widget_id} → fullscreen")
            self._saved_parent = self.parent()
            self._saved_position = getattr(self, 'grid_position', None)
            
            if self._saved_parent and self._saved_parent.layout():
                self._saved_parent.layout().removeWidget(self)

            self.setParent(None)
            self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
            self.showFullScreen()
            self.is_fullscreen = True
        except Exception:
            traceback.print_exc()

    def exit_fullscreen(self):
        if not self.is_fullscreen:
            return
        try:
            print(f"DEBUG: {self.widget_id} ← grid[{self._saved_position}]")
            
            self.setWindowFlags(Qt.WindowType.Widget)
            self.show()
            
            if self._saved_parent and self._saved_position:
                self.setParent(self._saved_parent)
                layout = self._saved_parent.layout()
                if layout:
                    layout.addWidget(self, *self._saved_position)
            
            self.is_fullscreen = False
            
            if self._saved_parent and self._saved_parent.window():
                self._saved_parent.window().showFullScreen()
        except Exception:
            traceback.print_exc()

    @pyqtSlot(object)
    def on_frame(self, frame):
        try:
            if frame is None:
                return
                
            if self.is_fullscreen:
                w, h = self.width(), self.height()
                if w > 0 and h > 0:
                    frame_resized = cv2.resize(frame, (w, h))
                else:
                    return
            else:
                frame_resized = cv2.resize(frame, (self.screen_width, self.screen_height))

            frame_rgb = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2RGB)
            h, w, ch = frame_rgb.shape
            bytes_per_line = ch * w
            img = QtGui.QImage(frame_rgb.data.tobytes(), w, h, bytes_per_line, 
                             QtGui.QImage.Format.Format_RGB888)
            pix = QtGui.QPixmap.fromImage(img)
            self.video_label.setPixmap(pix)
            self.frame_count += 1
        except Exception:
            pass

    @pyqtSlot(bool)
    def on_status_changed(self, online):
        if online:
            self.setStyleSheet(self.normal_style)
        else:
            self.video_label.clear()

    def reset_style(self):
        self.video_label.setStyleSheet("")
        self.setStyleSheet(self.normal_style)

    def _print_fps(self):
        try:
            now = time.time()
            elapsed = now - self.prev_time
            if elapsed >= 1.0:
                fps = self.frame_count / elapsed if elapsed > 0 else 0.0
                print(f"DEBUG: {self.widget_id} FPS: {fps:.1f}")
                self.frame_count = 0
                self.prev_time = now
        except:
            pass

    def cleanup(self):
        try:
            if hasattr(self, 'worker') and self.worker:
                self.worker.stop()
        except:
            pass

# === HELPER FUNCTIONS ===
def get_smart_grid(num_cameras):
    """Calculate best rows/cols for N cameras. Max 9."""
    if num_cameras <= 1: return 1, 1
    elif num_cameras == 2: return 1, 2
    elif num_cameras == 3: return 1, 3
    elif num_cameras == 4: return 2, 2
    elif num_cameras <= 6: return 2, 3
    elif num_cameras <= 9: return 3, 3
    else:
        cols = min(4, int(num_cameras**0.5 * 1.5))
        rows = (num_cameras + cols - 1) // cols
        return rows, cols

def find_working_cameras():
    """Test /dev/video0-4 + V4L2 devices with delays for RPi USB detection."""
    working = []
    try:
        enumerated = [cam.index for cam in enumerate_cameras(cv2.CAP_V4L2)]
        print(f"DEBUG: Enumerated V4L2 cameras: {enumerated}")
    except:
        enumerated = []
    
    test_indices = list(set([0,1,2,3,4] + enumerated))
    print(f"DEBUG: Testing camera indices: {test_indices}")
    
    for i in test_indices:
        if i in working: 
            continue
            
        print(f"DEBUG: Testing camera {i}...")
        try:
            time.sleep(0.5)  # USB enumeration delay
            
            cap = cv2.VideoCapture(i, cv2.CAP_V4L2)
            time.sleep(0.3)  # Camera init delay
            
            if cap.isOpened():
                time.sleep(0.2)  # Stabilization
                ret, test_frame = cap.read()
                if ret and test_frame is not None:
                    working.append(i)
                    print(f"DEBUG: Camera {i} confirmed working")
                else:
                    print(f"DEBUG: Camera {i} opened but no frame")
            else:
                print(f"DEBUG: Camera {i} failed to open")
                
            cap.release()
            time.sleep(0.5)  # Between tests
            
        except Exception as e:
            print(f"DEBUG: Camera {i} error: {e}")
            time.sleep(0.5)
    
    print(f"DEBUG: Confirmed {len(working)} working cameras: {working}")
    return working

def safe_cleanup(widgets):
    print("DEBUG: Cleaning all cameras")
    for w in widgets[:]:
        try:
            w.cleanup()
        except:
            pass

# === MAIN APPLICATION ===
def main():
    print("DEBUG: Starting camera grid app")
    app = QtWidgets.QApplication(sys.argv)
    camera_widgets = []

    def on_sigint(sig, frame):
        safe_cleanup(camera_widgets)
        sys.exit(0)
    signal.signal(signal.SIGINT, on_sigint)
    atexit.register(lambda: safe_cleanup(camera_widgets))

    try:
        app.setStyleSheet(qdarkstyle.load_stylesheet_pyqt6())
    except:
        app.setStyle(QtWidgets.QStyleFactory.create("Fusion"))

    mw = QtWidgets.QMainWindow()
    mw.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint)
    central_widget = QtWidgets.QWidget()
    central_widget.selected_camera = None
    mw.setCentralWidget(central_widget)
    mw.showFullScreen()

    screen = app.primaryScreen().availableGeometry()
    working_cameras = find_working_cameras()
    print(f"DEBUG: Found {len(working_cameras)} cameras")

    # 🔥 FINAL INITIALIZATION DELAY
    print("DEBUG: Final camera init delay...")
    time.sleep(1.0)

    layout = QtWidgets.QGridLayout(central_widget)
    layout.setContentsMargins(10,10,10,10)
    layout.setSpacing(10)

    if working_cameras:
        rows, cols = get_smart_grid(len(working_cameras))
        widget_width = screen.width() // cols
        widget_height = screen.height() // rows
        
        for cam_index in working_cameras[:9]:
            cw = CameraWidget(widget_width, widget_height, cam_index, parent=central_widget)
            camera_widgets.append(cw)

        for i, cw in enumerate(camera_widgets):
            row = i // cols
            col = i % cols
            cw.grid_position = (row, col)
            layout.addWidget(cw, row, col)
    else:
        label = QtWidgets.QLabel("NO CAMERAS FOUND")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("font-size: 24px; color: #888;")
        layout.addWidget(label, 0, 0)

    app.aboutToQuit.connect(lambda: safe_cleanup(camera_widgets))
    QtGui.QShortcut(QtGui.QKeySequence('Ctrl+Q'), mw, 
                   lambda: (safe_cleanup(camera_widgets), app.quit()))

    print("DEBUG: Short click=fullscreen toggle. Hold 400ms=swap mode. Ctrl+Q=quit.")
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
