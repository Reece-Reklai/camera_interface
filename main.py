# ============================================================
# TABLE OF CONTENTS
# ------------------------------------------------------------
# 1. DEBUG PRINTS
# 2. LOGGING
# 3. DYNAMIC PERFORMANCE TUNING
# 4. CAMERA RESCAN (HOT-PLUG SUPPORT)
# 5. FRAME POOL
# 6. CAMERA CAPTURE WORKER
# 7. FULLSCREEN OVERLAY
# 8. CAMERA WIDGET
# 9. GRID LAYOUT HELPERS
# 10. SYSTEM / PROCESS HELPERS
# 11. CAMERA DISCOVERY
# 12. CLEANUP + PROFILE SELECTION
# 13. MAIN ENTRYPOINT
# ============================================================

# ------------------------------------------------------------
# Standard library imports
# ------------------------------------------------------------
import atexit
import glob
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

# ------------------------------------------------------------
# Third-party imports
# ------------------------------------------------------------
import cv2
import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QThread, QTimer

# ============================================================
# DEBUG PRINTS (disabled by default)
# ------------------------------------------------------------
DEBUG_PRINTS = False

def dprint(*args, **kwargs):
    """Lightweight debug print wrapper."""
    if DEBUG_PRINTS:
        print(*args, **kwargs)

# ============================================================
# LOGGING
# ------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ============================================================
# DYNAMIC PERFORMANCE TUNING
# ------------------------------------------------------------
DYNAMIC_FPS_ENABLED = True
PERF_CHECK_INTERVAL_MS = 2000
MIN_DYNAMIC_FPS = 5
CPU_LOAD_THRESHOLD = 0.85
CPU_TEMP_THRESHOLD_C = 70.0
STRESS_HOLD_COUNT = 2
RECOVER_HOLD_COUNT = 3

# Reconnection settings
MAX_RECONNECT_BACKOFF_SEC = 10.0
RECONNECT_BACKOFF_MULTIPLIER = 1.5

# ============================================================
# CAMERA RESCAN (HOT-PLUG SUPPORT)
# ------------------------------------------------------------
RESCAN_INTERVAL_MS = 5000
FAILED_CAMERA_COOLDOWN_SEC = 30.0

def _read_cpu_load_ratio():
    """Read 1-minute load average normalized to CPU count."""
    try:
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        return min(1.0, load1 / cpu_count)
    except Exception:
        return None

def _read_cpu_temp_c():
    """Read CPU temperature in Celsius if the system exposes it."""
    paths = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/hwmon/hwmon0/temp1_input",
    ]
    for p in paths:
        try:
            if os.path.exists(p):
                with open(p, "r") as f:
                    raw = f.read().strip()
                if raw:
                    val = float(raw)
                    if val > 1000:
                        val = val / 1000.0
                    return val
        except Exception:
            continue
    return None

def _is_system_stressed():
    """
    Check CPU load or temperature thresholds.
    Returns: (stressed: bool, load_ratio: float|None, temp_c: float|None)
    """
    load_ratio = _read_cpu_load_ratio()
    temp_c = _read_cpu_temp_c()

    stressed = False
    if load_ratio is not None and load_ratio >= CPU_LOAD_THRESHOLD:
        stressed = True
    if temp_c is not None and temp_c >= CPU_TEMP_THRESHOLD_C:
        stressed = True

    return stressed, load_ratio, temp_c

# ============================================================
# FRAME POOL
# ------------------------------------------------------------
# Reuse numpy arrays to reduce allocation overhead.
# ============================================================
class FramePool:
    """Thread-safe pool of reusable numpy arrays."""
    
    def __init__(self, max_size=8):
        self._pool = deque(maxlen=max_size)
        self._lock = threading.Lock()
    
    def get(self, shape, dtype=np.uint8):
        """Get a buffer from pool or allocate new one."""
        with self._lock:
            try:
                frame = self._pool.pop()
                if frame.shape == shape and frame.dtype == dtype:
                    return frame
            except IndexError:
                pass
        return np.empty(shape, dtype=dtype)
    
    def release(self, frame):
        """Return a buffer to the pool for reuse."""
        if frame is not None:
            with self._lock:
                self._pool.append(frame)
    
    def clear(self):
        """Clear all pooled buffers."""
        with self._lock:
            self._pool.clear()

# Global frame pool shared across all capture workers
_frame_pool = FramePool(max_size=16)

# ============================================================
# CAMERA CAPTURE WORKER
# ------------------------------------------------------------
# Runs on its own QThread to avoid blocking the UI thread.
# ============================================================
class CaptureWorker(QThread):
    frame_ready = pyqtSignal(object)
    status_changed = pyqtSignal(bool)

    def __init__(
        self,
        stream_link,
        parent=None,
        maxlen=1,
        target_fps=None,
        capture_width=None,
        capture_height=None,
        downsample_max_dim=None,
    ):
        """Initialize camera capture settings and state."""
        super().__init__(parent)
        self.stream_link = stream_link
        self._running = True
        self._reconnect_backoff = 1.0
        self._cap = None
        self._last_emit = 0.0
        self._target_fps = target_fps
        self._emit_interval = 1.0 / 30.0
        self.capture_width = capture_width
        self.capture_height = capture_height
        self.downsample_max_dim = downsample_max_dim
        self.buffer = deque(maxlen=maxlen)
        
        # Atomic reads don't need locks in CPython for simple float/int
        # Using a simple attribute for emit_interval that UI can read
        self._cached_emit_interval = self._emit_interval

    def run(self):
        """Capture loop: open camera, grab frames, emit, reconnect on failure."""
        logging.info("Camera %s thread started", self.stream_link)
        
        while self._running:
            try:
                if not self._ensure_capture_open():
                    continue

                # Grab only - don't decode yet
                grabbed = self._cap.grab()
                if not grabbed:
                    self._handle_capture_failure()
                    continue

                # Check if we should emit before expensive decode
                now = time.monotonic()
                emit_interval = self._cached_emit_interval
                
                time_since_emit = now - self._last_emit
                if time_since_emit < emit_interval:
                    # Sleep smartly instead of fixed 1ms
                    sleep_ms = max(1, int((emit_interval - time_since_emit) * 1000) - 1)
                    self.msleep(min(sleep_ms, 10))
                    continue

                # Now decode - only when we'll actually use the frame
                ret, frame = self._cap.retrieve()
                if not ret or frame is None:
                    self._handle_capture_failure()
                    continue

                # Downsample in capture thread to reduce UI load
                if self.downsample_max_dim:
                    frame = self._downsample_frame(frame)

                self.buffer.append(frame)
                self.frame_ready.emit(frame)
                self._last_emit = now

            except Exception:
                logging.exception("Exception in CaptureWorker %s", self.stream_link)
                time.sleep(0.2)

        self._close_capture()
        logging.info("Camera %s thread stopped", self.stream_link)

    def _ensure_capture_open(self):
        """Ensure capture is open; reconnect if needed. Returns True if ready."""
        if self._cap is not None and self._cap.isOpened():
            return True
        
        self._open_capture()
        if self._cap and self._cap.isOpened():
            self._reconnect_backoff = 1.0
            self.status_changed.emit(True)
            return True
        
        time.sleep(self._reconnect_backoff)
        self._reconnect_backoff = min(
            self._reconnect_backoff * RECONNECT_BACKOFF_MULTIPLIER,
            MAX_RECONNECT_BACKOFF_SEC
        )
        return False

    def _handle_capture_failure(self):
        """Handle capture read failure."""
        self._close_capture()
        self.status_changed.emit(False)

    def _downsample_frame(self, frame):
        """Downsample frame if larger than max dimension."""
        h, w = frame.shape[:2]
        max_dim = self.downsample_max_dim
        
        if w <= max_dim and h <= max_dim:
            return frame
        
        scale = max_dim / max(w, h)
        new_w = int(w * scale)
        new_h = int(h * scale)
        
        # Use INTER_NEAREST for speed, INTER_LINEAR for quality
        return cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_NEAREST)

    def _open_capture(self):
        """Open the camera and apply preferred capture settings."""
        try:
            backend = cv2.CAP_V4L2 if platform.system() == "Linux" else cv2.CAP_ANY
            cap = cv2.VideoCapture(self.stream_link, backend)
            
            if not cap or not cap.isOpened():
                self._safe_release(cap)
                return

            # Request MJPEG to reduce decode overhead
            try:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
            except Exception:
                pass

            # Apply capture resolution
            if self.capture_width:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.capture_width))
            if self.capture_height:
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.capture_height))

            # Minimize internal buffering
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass

            # Set FPS
            try:
                fps = float(self._target_fps) if self._target_fps and self._target_fps > 0 else 0
                cap.set(cv2.CAP_PROP_FPS, fps)
            except Exception:
                pass

            if cap.isOpened():
                self._cap = cap
                self._configure_fps_from_camera()
                logging.info(
                    "Opened capture %s (requested %sx%s) -> emit fps=%.1f",
                    self.stream_link,
                    self.capture_width,
                    self.capture_height,
                    1.0 / self._emit_interval if self._emit_interval > 0 else 0.0,
                )
            else:
                self._safe_release(cap)
        except Exception:
            logging.exception("Failed to open capture %s", self.stream_link)

    def _configure_fps_from_camera(self):
        """Pick a usable FPS value and update emit interval."""
        if self._target_fps and self._target_fps > 0:
            fps = float(self._target_fps)
        else:
            fps = float(self._cap.get(cv2.CAP_PROP_FPS)) if self._cap else 0.0

        if fps <= 1.0 or fps > 240.0:
            fps = 30.0

        self._emit_interval = 1.0 / max(1.0, fps)
        self._cached_emit_interval = self._emit_interval

    def set_target_fps(self, fps):
        """Update target FPS and camera setting at runtime."""
        if fps is None:
            return
        try:
            fps = float(fps)
            if fps <= 0:
                return
            
            self._target_fps = fps
            self._emit_interval = 1.0 / max(1.0, fps)
            self._cached_emit_interval = self._emit_interval
            
            if self._cap:
                try:
                    self._cap.set(cv2.CAP_PROP_FPS, fps)
                except Exception:
                    pass
        except Exception:
            logging.exception("set_target_fps")

    def set_downsample_max_dim(self, max_dim):
        """Update downsample dimension at runtime."""
        self.downsample_max_dim = max_dim

    def _safe_release(self, cap):
        """Safely release a capture object."""
        try:
            if cap:
                cap.release()
        except Exception:
            pass

    def _close_capture(self):
        """Release camera handle if open."""
        self._safe_release(self._cap)
        self._cap = None

    def stop(self):
        """Stop capture loop and wait for thread exit."""
        self._running = False
        self.requestInterruption()
        self.wait(2000)
        self._close_capture()

# ============================================================
# FULLSCREEN OVERLAY
# ------------------------------------------------------------
class FullscreenOverlay(QtWidgets.QWidget):
    def __init__(self, on_click_exit):
        """Create a full-window view with a centered QLabel."""
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.on_click_exit = on_click_exit
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background:black;")
        self.label = QtWidgets.QLabel(self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setScaledContents(False)  # We handle scaling ourselves
        self.label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Ignored
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)

    def mousePressEvent(self, event):
        """Exit fullscreen on left click/tap."""
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.on_click_exit()
        super().mousePressEvent(event)

# ============================================================
# CAMERA WIDGET
# ------------------------------------------------------------
class CameraWidget(QtWidgets.QWidget):
    hold_threshold_ms = 400

    def __init__(
        self,
        width,
        height,
        stream_link=0,
        aspect_ratio=False,
        parent=None,
        buffer_size=1,
        target_fps=None,
        request_capture_size=(640, 480),
        ui_fps=15,
        enable_capture=True,
        placeholder_text=None,
        settings_mode=False,
        on_restart=None,
        downsample_max_dim=None,
    ):
        """Initialize tile UI, worker thread, and timers."""
        super().__init__(parent)
        logging.debug("Creating camera %s", stream_link)

        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setMouseTracking(True)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding
        )

        self.screen_width = max(1, width)
        self.screen_height = max(1, height)
        self.maintain_aspect_ratio = aspect_ratio
        self.camera_stream_link = stream_link
        self.widget_id = f"cam{stream_link}_{id(self)}"

        self.is_fullscreen = False
        self.grid_position = None
        self._press_widget_id = None
        self._press_time = 0.0
        self._grid_parent = None
        self._touch_active = False
        self.swap_active = False
        self._fs_overlay = None

        self.capture_enabled = bool(enable_capture)
        self.placeholder_text = placeholder_text
        self.settings_mode = settings_mode
        self.downsample_max_dim = downsample_max_dim

        self.normal_style = "border: 2px solid #555; background: black;"
        self.swap_ready_style = "border: 4px solid #FFFF00; background: black;"
        self.setStyleSheet(self.normal_style)
        self.setObjectName(self.widget_id)

        self.video_label = QtWidgets.QLabel(self)
        self.video_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding
        )
        self.video_label.setMinimumSize(1, 1)
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setScaledContents(False)  # We handle scaling
        self.video_label.setMouseTracking(True)
        self.video_label.setObjectName(f"{self.widget_id}_label")
        self.video_label.setAttribute(QtCore.Qt.WidgetAttribute.WA_AcceptTouchEvents, True)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if self.settings_mode:
            self._setup_settings_mode(layout, on_restart)
        else:
            layout.addWidget(self.video_label)

        # Frame tracking
        self.frame_count = 0
        self.prev_time = time.monotonic()
        self._latest_frame = None
        self._last_rendered_frame = None  # Track to skip unchanged frames
        self._frame_id = 0  # Incremented on new frame

        self.base_target_fps = target_fps
        self.current_target_fps = target_fps

        # Start capture worker
        self.worker = None
        if self.capture_enabled and stream_link is not None:
            cap_w, cap_h = request_capture_size if request_capture_size else (None, None)
            self.worker = CaptureWorker(
                stream_link,
                parent=self,
                maxlen=buffer_size,
                target_fps=target_fps,
                capture_width=cap_w,
                capture_height=cap_h,
                downsample_max_dim=downsample_max_dim,
            )
            self.worker.frame_ready.connect(self.on_frame)
            self.worker.status_changed.connect(self.on_status_changed)
            self.worker.start()
        elif not self.settings_mode:
            self._latest_frame = None
            self._render_placeholder(self.placeholder_text or "DISCONNECTED")

        # UI render timer - decoupled from capture FPS
        if not self.settings_mode:
            self.ui_render_fps = max(1, int(ui_fps))
            self.render_timer = QTimer(self)
            self.render_timer.setInterval(int(1000 / self.ui_render_fps))
            self.render_timer.timeout.connect(self._render_latest_frame)
            self.render_timer.start()
        else:
            self.ui_render_fps = 0
            self.render_timer = None

        # FPS diagnostics timer
        self.ui_timer = None  # Created by main for batch logging

        self.installEventFilter(self)
        self.video_label.installEventFilter(self)

        logging.debug("Widget %s ready", self.widget_id)

    def _setup_settings_mode(self, layout, on_restart):
        """Configure widget as settings tile."""
        self.video_label.setText(self.placeholder_text or "SETTINGS")
        self.video_label.setStyleSheet("color: #ffffff; font-size: 20px;")

        restart_button = QtWidgets.QPushButton("Restart")
        restart_button.setStyleSheet(
            "QPushButton { padding: 10px 16px; font-size: 18px; }"
        )
        if on_restart:
            restart_button.clicked.connect(on_restart)

        layout.addStretch(1)
        layout.addWidget(self.video_label)
        layout.addSpacing(12)
        layout.addWidget(restart_button, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)

    def _ensure_fullscreen_overlay(self):
        """Create fullscreen overlay only when needed."""
        if self._fs_overlay is None:
            self._fs_overlay = FullscreenOverlay(self.exit_fullscreen)

    def _apply_ui_fps(self, ui_fps):
        """Update UI render timer to match camera UI FPS."""
        self.ui_render_fps = max(1, int(ui_fps))
        if self.render_timer:
            self.render_timer.setInterval(int(1000 / self.ui_render_fps))

    def attach_camera(self, stream_link, target_fps, request_capture_size, ui_fps=None):
        """Attach a camera to an existing placeholder slot."""
        if self.capture_enabled and self.worker:
            return

        self.capture_enabled = True
        self.camera_stream_link = stream_link
        self.base_target_fps = target_fps
        self.current_target_fps = target_fps

        if ui_fps is not None:
            self._apply_ui_fps(ui_fps)

        cap_w, cap_h = request_capture_size if request_capture_size else (None, None)
        self.worker = CaptureWorker(
            stream_link,
            parent=self,
            maxlen=1,
            target_fps=target_fps,
            capture_width=cap_w,
            capture_height=cap_h,
            downsample_max_dim=self.downsample_max_dim,
        )
        self.worker.frame_ready.connect(self.on_frame)
        self.worker.status_changed.connect(self.on_status_changed)
        self.worker.start()

        self._latest_frame = None
        self._render_placeholder("CONNECTING...")
        logging.info("Attached camera %s to widget %s", stream_link, self.widget_id)

    def eventFilter(self, obj, event):
        """Handle touch and mouse events from widget or label."""
        if obj not in (self, self.video_label):
            return super().eventFilter(obj, event)

        etype = event.type()
        if etype == QtCore.QEvent.Type.TouchBegin:
            return self._on_touch_begin(event)
        if etype == QtCore.QEvent.Type.TouchEnd:
            return self._on_touch_end(event)
        if etype == QtCore.QEvent.Type.MouseButtonPress:
            return self._on_mouse_press(event)
        if etype == QtCore.QEvent.Type.MouseButtonRelease:
            return self._on_mouse_release(event)
        
        return super().eventFilter(obj, event)

    def _on_touch_begin(self, event):
        """Record touch-down timestamp and source widget."""
        try:
            if event.points() and len(event.points()) == 1:
                self._touch_active = True
                self._press_time = time.monotonic() * 1000.0
                self._press_widget_id = self.widget_id
                self._grid_parent = self.parent()
                logging.debug("Touch begin %s", self.widget_id)
        except Exception:
            logging.exception("touch begin")
        return True

    def _on_touch_end(self, event):
        """Handle touch-up as a click/hold action."""
        try:
            if self._touch_active:
                self._touch_active = False
                self._handle_press_release()
        except Exception:
            logging.exception("touch end")
        return True

    def _on_mouse_press(self, event):
        """Record mouse down position and time."""
        try:
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                self._press_time = time.monotonic() * 1000.0
                self._press_widget_id = self.widget_id
                self._grid_parent = self.parent()
                logging.debug("Press %s", self.widget_id)
            elif event.button() == QtCore.Qt.MouseButton.RightButton:
                self.toggle_fullscreen()
        except Exception:
            logging.exception("mouse press")
        return True

    def _on_mouse_release(self, event):
        """Handle mouse release as click/hold action."""
        try:
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                if self._press_widget_id == self.widget_id:
                    self._handle_press_release()
        except Exception:
            logging.exception("mouse release")
        return True

    def _handle_press_release(self):
        """
        Unified release handler for both touch and mouse:
        - short tap: fullscreen toggle
        - long press: swap select
        - swap if another camera is selected
        """
        try:
            if not self._press_widget_id or self._press_widget_id != self.widget_id:
                return

            hold_time = (time.monotonic() * 1000.0) - self._press_time
            logging.debug("Release %s hold=%dms", self.widget_id, int(hold_time))

            swap_parent = self._grid_parent
            if not swap_parent or not hasattr(swap_parent, 'selected_camera'):
                self._reset_mouse_state()
                self.toggle_fullscreen()
                return

            # Clicking on already-selected camera clears selection
            if swap_parent.selected_camera == self:
                logging.debug("Clear swap %s", self.widget_id)
                swap_parent.selected_camera = None
                self.swap_active = False
                self.reset_style()
                self._reset_mouse_state()
                return

            # Clicking on different camera while one is selected = swap
            if (swap_parent.selected_camera and 
                    swap_parent.selected_camera != self and 
                    not self.is_fullscreen):
                other = swap_parent.selected_camera
                logging.debug("SWAP %s <-> %s", other.widget_id, self.widget_id)
                self.do_swap(other, self, swap_parent)
                other.swap_active = False
                other.reset_style()
                swap_parent.selected_camera = None
                self._reset_mouse_state()
                return

            # Long press = enter swap mode
            if hold_time >= self.hold_threshold_ms and not self.is_fullscreen:
                logging.debug("ENTER swap %s", self.widget_id)
                swap_parent.selected_camera = self
                self.swap_active = True
                self.setStyleSheet(self.swap_ready_style)
                self._reset_mouse_state()
                return

            # Short tap = fullscreen toggle
            logging.debug("Short tap fullscreen %s", self.widget_id)
            self.toggle_fullscreen()

        except Exception:
            logging.exception("press release")
        finally:
            self._reset_mouse_state()

    def _reset_mouse_state(self):
        """Clear press state to avoid accidental reuse."""
        self._press_time = 0
        self._press_widget_id = None
        self._grid_parent = None

    def do_swap(self, source, target, layout_parent):
        """Swap two widgets inside the grid layout."""
        try:
            source_pos = getattr(source, 'grid_position', None)
            target_pos = getattr(target, 'grid_position', None)
            if source_pos is None or target_pos is None:
                logging.debug("Swap failed - missing positions")
                return

            layout = layout_parent.layout()
            layout.removeWidget(source)
            layout.removeWidget(target)
            layout.addWidget(target, *source_pos)
            layout.addWidget(source, *target_pos)
            source.grid_position, target.grid_position = target_pos, source_pos
            logging.debug("Swap complete %s <-> %s", source.widget_id, target.widget_id)
        except Exception:
            logging.exception("do_swap")

    def toggle_fullscreen(self):
        """Toggle between fullscreen and grid view."""
        if self.is_fullscreen:
            self.exit_fullscreen()
        else:
            self.go_fullscreen()

    def go_fullscreen(self):
        """Enter fullscreen mode for this camera."""
        if self.is_fullscreen:
            return
        self._ensure_fullscreen_overlay()

        screen = QtWidgets.QApplication.primaryScreen()
        if screen:
            self._fs_overlay.setGeometry(screen.geometry())

        self._fs_overlay.showFullScreen()
        self._fs_overlay.raise_()
        self._fs_overlay.activateWindow()
        self.is_fullscreen = True

        # Force re-render for fullscreen
        self._last_rendered_frame = None

        if self._latest_frame is None and not self.settings_mode:
            self._render_placeholder(self.placeholder_text or "DISCONNECTED")

    def exit_fullscreen(self):
        """Exit fullscreen and return to grid view."""
        if not self.is_fullscreen:
            return
        if self._fs_overlay:
            self._fs_overlay.hide()
        self.is_fullscreen = False
        # Force re-render for grid
        self._last_rendered_frame = None

    @pyqtSlot(object)
    def on_frame(self, frame_bgr):
        """Receive latest camera frame from worker."""
        try:
            if frame_bgr is None:
                return
            self._latest_frame = frame_bgr
            self._frame_id += 1
        except Exception:
            logging.exception("on_frame")

    def _render_placeholder(self, text):
        """Render placeholder text when no frame is available."""
        if self.settings_mode:
            return
        
        target_label = (
            self._fs_overlay.label 
            if (self.is_fullscreen and self._fs_overlay) 
            else self.video_label
        )
        target_label.setPixmap(QtGui.QPixmap())
        target_label.setText(text)
        target_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        target_label.setStyleSheet("color: #bbbbbb; font-size: 24px;")
        
        if self.swap_active:
            self.setStyleSheet(self.swap_ready_style)

    def _render_latest_frame(self):
        """Convert latest frame to QPixmap and display it."""
        if self.settings_mode:
            return
        
        try:
            frame_bgr = self._latest_frame
            
            if frame_bgr is None:
                if self._last_rendered_frame is not None:
                    self._render_placeholder(self.placeholder_text or "DISCONNECTED")
                    self._last_rendered_frame = None
                return

            # Skip if frame hasn't changed
            current_frame_id = self._frame_id
            if current_frame_id == self._last_rendered_frame:
                return
            self._last_rendered_frame = current_frame_id

            # Determine target label and size
            if self.is_fullscreen and self._fs_overlay:
                target_label = self._fs_overlay.label
                target_size = self._fs_overlay.size()
                use_smooth = True
            else:
                target_label = self.video_label
                target_size = self.video_label.size()
                use_smooth = False  # Fast transform for grid tiles

            # Convert numpy frame to Qt image
            if frame_bgr.ndim == 2:
                h, w = frame_bgr.shape
                bytes_per_line = w
                fmt = QtGui.QImage.Format.Format_Grayscale8
            else:
                h, w, ch = frame_bgr.shape
                bytes_per_line = ch * w
                fmt = QtGui.QImage.Format.Format_BGR888

            # Create QImage and immediately copy to own the data
            img = QtGui.QImage(frame_bgr.data, w, h, bytes_per_line, fmt).copy()
            pix = QtGui.QPixmap.fromImage(img)

            # Scale to target size
            if target_size.width() > 0 and target_size.height() > 0:
                transform = (
                    Qt.TransformationMode.SmoothTransformation 
                    if use_smooth 
                    else Qt.TransformationMode.FastTransformation
                )
                pix = pix.scaled(
                    target_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    transform
                )

            target_label.setPixmap(pix)
            target_label.setText("")
            target_label.setStyleSheet("")

            self.frame_count += 1

        except Exception:
            logging.exception("render frame")

    @pyqtSlot(bool)
    def on_status_changed(self, online):
        """Update UI when camera goes online or offline."""
        if online:
            self.setStyleSheet(self.normal_style)
            self.video_label.setText("")
        else:
            self._latest_frame = None
            self._last_rendered_frame = None
            self._render_placeholder("DISCONNECTED")

    def reset_style(self):
        """Restore default border styling."""
        self.video_label.setStyleSheet("")
        self.setStyleSheet(self.swap_ready_style if self.swap_active else self.normal_style)

    def get_fps_stats(self):
        """Get current FPS and reset counter. Called by main for batch logging."""
        now = time.monotonic()
        elapsed = now - self.prev_time
        if elapsed >= 0.5:
            fps = self.frame_count / elapsed if elapsed > 0 else 0.0
            self.frame_count = 0
            self.prev_time = now
            return fps
        return None

    def set_dynamic_fps(self, fps):
        """Apply dynamic FPS change from stress monitor."""
        if fps is None or not self.capture_enabled:
            return
        try:
            fps = float(fps)
            if fps < MIN_DYNAMIC_FPS:
                fps = MIN_DYNAMIC_FPS
            self.current_target_fps = fps
            if self.worker:
                self.worker.set_target_fps(fps)
        except Exception:
            logging.exception("set_dynamic_fps")

    def cleanup(self):
        """Stop the capture worker thread cleanly."""
        try:
            if self.render_timer:
                self.render_timer.stop()
            if hasattr(self, 'worker') and self.worker:
                self.worker.stop()
        except Exception:
            pass

# ============================================================
# GRID LAYOUT HELPERS
# ------------------------------------------------------------
def get_smart_grid(num_cameras):
    """Return a sensible grid (rows, cols) for N cameras."""
    if num_cameras <= 1:
        return 1, 1
    elif num_cameras == 2:
        return 1, 2
    elif num_cameras == 3:
        return 1, 3
    elif num_cameras == 4:
        return 2, 2
    elif num_cameras <= 6:
        return 2, 3
    elif num_cameras <= 9:
        return 3, 3
    else:
        cols = min(4, int(num_cameras ** 0.5 * 1.5))
        rows = (num_cameras + cols - 1) // cols
        return rows, cols

# ============================================================
# SYSTEM / PROCESS HELPERS
# ------------------------------------------------------------
def _run_cmd(cmd):
    """Run a shell command and return stdout, stderr, returncode."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=2)
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except Exception:
        return "", "", 1

def _get_pids_from_lsof(device_path):
    """Get PIDs holding device using lsof."""
    out, _, code = _run_cmd(f"lsof -t {device_path}")
    if code != 0 or not out:
        return set()
    pids = set()
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.add(int(line))
    return pids

def _get_pids_from_fuser(device_path):
    """Get PIDs holding device using fuser."""
    out, _, code = _run_cmd(f"fuser -v {device_path}")
    if code != 0 or not out:
        return set()
    pids = set()
    for match in re.findall(r"\b(\d+)\b", out):
        pids.add(int(match))
    return pids

def _is_pid_alive(pid):
    """Check if a PID exists."""
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False

def kill_device_holders(device_path, grace=0.4):
    """Attempt to terminate any process holding a camera device."""
    pids = _get_pids_from_lsof(device_path)
    if not pids:
        pids = _get_pids_from_fuser(device_path)

    pids.discard(os.getpid())
    if not pids:
        return False

    logging.info("Killing holders of %s: %s", device_path, sorted(pids))

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except PermissionError:
            _run_cmd(f"sudo fuser -k {device_path}")
            break
        except Exception:
            pass

    time.sleep(grace)

    for pid in list(pids):
        if _is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except PermissionError:
                _run_cmd(f"sudo fuser -k {device_path}")
            except Exception:
                pass

    return True

# ============================================================
# CAMERA DISCOVERY
# ------------------------------------------------------------
def test_single_camera(
    cam_index,
    retries=3,
    retry_delay=0.2,
    allow_kill=True,
    post_kill_retries=2,
    post_kill_delay=0.25,
):
    """Try to open and grab a frame from one camera index."""
    device_path = f"/dev/video{cam_index}"

    def try_open():
        cap = cv2.VideoCapture(cam_index, cv2.CAP_V4L2)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                return False
            if not cap.grab():
                return False
            return True
        finally:
            try:
                cap.release()
            except Exception:
                pass

    for _ in range(retries):
        if try_open():
            return cam_index
        time.sleep(retry_delay)

    if allow_kill:
        killed = kill_device_holders(device_path)
        if killed:
            for _ in range(post_kill_retries):
                if try_open():
                    return cam_index
                time.sleep(post_kill_delay)

    return None

def get_video_indexes():
    """List integer indices for /dev/video* devices."""
    video_devices = glob.glob('/dev/video*')
    indexes = []
    for device in sorted(video_devices):
        try:
            index = int(device.split('video')[-1])
            indexes.append(index)
        except Exception:
            pass
    return indexes

def find_working_cameras():
    """Return a list of camera indices that can capture frames."""
    indexes = get_video_indexes()
    if not indexes:
        logging.info("No /dev/video* devices found!")
        return []

    max_workers = min(4, len(indexes))
    logging.info("Testing %d cameras concurrently (workers=%d)...", len(indexes), max_workers)
    working = []
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(test_single_camera, idx) for idx in indexes]
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                with lock:
                    working.append(result)
                    logging.info("Camera %d OK", result)

    if working:
        logging.info("Round 2 - Double-check (no pre-kill)...")
        final_working = []
        with ThreadPoolExecutor(max_workers=min(4, len(working))) as executor:
            futures = [
                executor.submit(
                    test_single_camera, idx, retries=2, retry_delay=0.15, allow_kill=False
                )
                for idx in working
            ]
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    final_working.append(result)
                    logging.info("Confirmed camera %d", result)
        working = final_working

    logging.info("FINAL Working cameras: %s", working)
    return working

# ============================================================
# CLEANUP + PROFILE SELECTION
# ------------------------------------------------------------
def safe_cleanup(widgets):
    """Gracefully stop all camera worker threads."""
    logging.info("Cleaning all cameras")
    for w in list(widgets):
        try:
            w.cleanup()
        except Exception:
            pass
    _frame_pool.clear()

def choose_profile():
    """Pick capture resolution, FPS, and downsample based on camera count."""
    return {
        'capture_size': (640, 480),
        'capture_fps': 20,
        'ui_fps': 25,
        'downsample_max_dim': 640,
    }

# ============================================================
# MAIN ENTRYPOINT
# ------------------------------------------------------------
def main():
    """Create the UI, discover cameras, and start event loop."""
    logging.info("Starting camera grid app")
    app = QtWidgets.QApplication(sys.argv)
    camera_widgets = []
    all_widgets = []
    placeholder_slots = []

    CAMERA_SLOT_COUNT = 3

    def on_sigint(sig, frame):
        safe_cleanup(camera_widgets)
        sys.exit(0)

    signal.signal(signal.SIGINT, on_sigint)
    atexit.register(lambda: safe_cleanup(camera_widgets))

    # Allow Python to handle SIGINT properly in Qt event loop
    sigint_timer = QTimer()
    sigint_timer.timeout.connect(lambda: None)
    sigint_timer.start(500)

    app.setStyle(QtWidgets.QStyleFactory.create("Fusion"))
    app.setStyleSheet("QWidget { background: #2b2b2b; color: #ffffff; }")

    mw = QtWidgets.QMainWindow()
    mw.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint)
    central_widget = QtWidgets.QWidget()
    central_widget.selected_camera = None
    mw.setCentralWidget(central_widget)

    mw.show()

    def force_fullscreen():
        mw.showFullScreen()
        mw.raise_()
        mw.activateWindow()

    QtCore.QTimer.singleShot(50, force_fullscreen)
    QtCore.QTimer.singleShot(300, force_fullscreen)

    screen = app.primaryScreen().availableGeometry()
    working_cameras = find_working_cameras()
    logging.info("Found %d cameras", len(working_cameras))

    known_indexes = set(get_video_indexes())
    active_indexes = set(working_cameras)
    failed_indexes = {idx: time.monotonic() for idx in (known_indexes - active_indexes)}

    layout = QtWidgets.QGridLayout(central_widget)
    layout.setContentsMargins(10, 10, 10, 10)
    layout.setSpacing(10)

    def restart_app():
        """Restart the entire process."""
        logging.info("Restart requested from settings.")
        safe_cleanup(camera_widgets)
        python = sys.executable
        os.execv(python, [python] + sys.argv)

    # Settings tile
    settings_tile = CameraWidget(
        width=1,
        height=1,
        stream_link=None,
        parent=central_widget,
        buffer_size=1,
        target_fps=None,
        request_capture_size=None,
        ui_fps=5,
        enable_capture=False,
        placeholder_text="SETTINGS",
        settings_mode=True,
        on_restart=restart_app,
    )
    all_widgets.append(settings_tile)

    profile = choose_profile()
    cap_w, cap_h = profile['capture_size']
    cap_fps = profile['capture_fps']
    ui_fps = profile['ui_fps']
    downsample_max_dim = profile['downsample_max_dim']
    
    logging.info(
        "Profile: %dx%d @ %d FPS (UI %d FPS, downsample=%s)",
        cap_w, cap_h, cap_fps, ui_fps, downsample_max_dim
    )

    # Create camera slots
    for slot_idx in range(CAMERA_SLOT_COUNT):
        if slot_idx < len(working_cameras):
            cam_index = working_cameras[slot_idx]
            cw = CameraWidget(
                1,
                1,
                cam_index,
                parent=central_widget,
                buffer_size=1,
                target_fps=cap_fps,
                request_capture_size=(cap_w, cap_h),
                ui_fps=ui_fps,
                enable_capture=True,
                downsample_max_dim=downsample_max_dim,
            )
            camera_widgets.append(cw)
        else:
            cw = CameraWidget(
                1,
                1,
                stream_link=None,
                parent=central_widget,
                buffer_size=1,
                target_fps=None,
                request_capture_size=None,
                ui_fps=5,
                enable_capture=False,
                placeholder_text="DISCONNECTED",
            )
            placeholder_slots.append(cw)
        all_widgets.append(cw)

    rows, cols = get_smart_grid(len(all_widgets))
    widget_width = max(1, screen.width() // cols)
    widget_height = max(1, screen.height() // rows)

    for cw in all_widgets:
        cw.screen_width = widget_width
        cw.screen_height = widget_height

    for i, cw in enumerate(all_widgets):
        row = i // cols
        col = i % cols
        cw.grid_position = (row, col)
        layout.addWidget(cw, row, col)

    for r in range(rows):
        layout.setRowStretch(r, 1)
    for c in range(cols):
        layout.setColumnStretch(c, 1)

    # Batch FPS logging - single timer for all cameras
    def log_all_fps():
        if not camera_widgets:
            return
        stats = {}
        for w in camera_widgets:
            fps = w.get_fps_stats()
            if fps is not None:
                stats[w.widget_id] = f"{fps:.1f}"
        if stats:
            logging.info("FPS: %s", stats)

    fps_log_timer = QTimer(mw)
    fps_log_timer.setInterval(2000)
    fps_log_timer.timeout.connect(log_all_fps)
    fps_log_timer.start()

    # Dynamic FPS adjustment
    if DYNAMIC_FPS_ENABLED and camera_widgets:
        stress_counter = {"stress": 0, "recover": 0}

        def adjust_fps():
            stressed, load_ratio, temp_c = _is_system_stressed()

            if stressed:
                stress_counter["stress"] += 1
                stress_counter["recover"] = 0
            else:
                stress_counter["recover"] += 1
                stress_counter["stress"] = 0

            if stress_counter["stress"] >= STRESS_HOLD_COUNT:
                for w in camera_widgets:
                    base = w.base_target_fps or 30
                    cur = w.current_target_fps or base
                    new_fps = max(MIN_DYNAMIC_FPS, cur - 2)
                    if new_fps < cur:
                        w.set_dynamic_fps(new_fps)
                stress_counter["stress"] = 0
                logging.info(
                    "Stress detected (load=%s, temp=%s). Lowering FPS.",
                    f"{load_ratio:.2f}" if load_ratio is not None else "n/a",
                    f"{temp_c:.1f}C" if temp_c is not None else "n/a"
                )

            if stress_counter["recover"] >= RECOVER_HOLD_COUNT:
                for w in camera_widgets:
                    base = w.base_target_fps or 30
                    cur = w.current_target_fps or base
                    new_fps = min(base, cur + 2)
                    if new_fps > cur:
                        w.set_dynamic_fps(new_fps)
                stress_counter["recover"] = 0
                logging.info("System stable. Restoring FPS.")

        perf_timer = QTimer(mw)
        perf_timer.setInterval(PERF_CHECK_INTERVAL_MS)
        perf_timer.timeout.connect(adjust_fps)
        perf_timer.start()

    # Background rescan for hot-plug
    if placeholder_slots:
        def rescan_and_attach():
            if not placeholder_slots:
                return

            now = time.monotonic()
            indexes = get_video_indexes()

            candidates = []
            for idx in indexes:
                if idx in active_indexes:
                    continue
                last_failed = failed_indexes.get(idx)
                if last_failed and (now - last_failed) < FAILED_CAMERA_COOLDOWN_SEC:
                    continue
                candidates.append(idx)

            if not candidates:
                return

            for idx in candidates:
                if not placeholder_slots:
                    break

                ok = test_single_camera(
                    idx,
                    retries=2,
                    retry_delay=0.15,
                    allow_kill=False,
                )
                if ok is not None:
                    slot = placeholder_slots.pop(0)
                    slot.downsample_max_dim = downsample_max_dim
                    slot.attach_camera(ok, cap_fps, (cap_w, cap_h), ui_fps=ui_fps)
                    camera_widgets.append(slot)
                    active_indexes.add(ok)
                    failed_indexes.pop(ok, None)
                    logging.info("Attached camera %d to empty slot", ok)
                else:
                    failed_indexes[idx] = now

        rescan_timer = QTimer(mw)
        rescan_timer.setInterval(RESCAN_INTERVAL_MS)
        rescan_timer.timeout.connect(rescan_and_attach)
        rescan_timer.start()

    app.aboutToQuit.connect(lambda: safe_cleanup(camera_widgets))
    QtGui.QShortcut(
        QtGui.QKeySequence('q'), mw,
        lambda: (safe_cleanup(camera_widgets), app.quit())
    )

    logging.info("Short click=fullscreen toggle. Hold 400ms=swap mode. Ctrl+Q=quit.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()