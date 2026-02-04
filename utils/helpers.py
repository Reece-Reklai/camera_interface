"""
Utility functions for Camera Dashboard.

Includes system helpers, process management, and systemd integration.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import socket
import subprocess
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ui.widgets import CameraWidget


def run_cmd(cmd: str, timeout: int = 2) -> tuple[str, str, int]:
    """Run a shell command and return stdout, stderr, returncode."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except Exception:
        return "", "", 1


def get_pids_from_lsof(device_path: str) -> set[int]:
    """Get PIDs holding device using lsof."""
    out, _, code = run_cmd(f"lsof -t {device_path}")
    if code != 0 or not out:
        return set()
    pids: set[int] = set()
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.add(int(line))
    return pids


def get_pids_from_fuser(device_path: str) -> set[int]:
    """Get PIDs holding device using fuser."""
    out, _, code = run_cmd(f"fuser -v {device_path}")
    if code != 0 or not out:
        return set()
    pids: set[int] = set()
    for match in re.findall(r"\b(\d+)\b", out):
        pids.add(int(match))
    return pids


def is_pid_alive(pid: int) -> bool:
    """Check if a PID exists."""
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def kill_device_holders(device_path: str, grace: float = 0.4) -> bool:
    """
    Attempt to terminate any process holding a camera device.
    Useful for kiosk-style setups.
    """
    from core import config
    
    if not config.KILL_DEVICE_HOLDERS:
        return False
        
    pids = get_pids_from_lsof(device_path)
    if not pids:
        pids = get_pids_from_fuser(device_path)

    pids.discard(os.getpid())
    if not pids:
        return False

    logging.info("Killing holders of %s: %s", device_path, sorted(pids))

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except PermissionError:
            run_cmd(f"sudo fuser -k {device_path}")
            break
        except Exception:
            pass

    time.sleep(grace)

    for pid in list(pids):
        if is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except PermissionError:
                run_cmd(f"sudo fuser -k {device_path}")
            except Exception:
                pass

    return True


def systemd_notify(message: str) -> None:
    """Send a notification to systemd."""
    try:
        sock_path = os.environ.get("NOTIFY_SOCKET")
        if not sock_path:
            return
        if sock_path[0] == "@":
            sock_path = "\0" + sock_path[1:]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.connect(sock_path)
        sock.sendall(message.encode("utf-8"))
        sock.close()
    except Exception:
        logging.debug("systemd notify failed")


def write_watchdog_heartbeat() -> None:
    """Write watchdog heartbeat if running under systemd."""
    if os.getenv("WATCHDOG_USEC") is None:
        return
    systemd_notify("WATCHDOG=1")


def log_health_summary(
    camera_widgets: list[CameraWidget],
    placeholder_slots: list[CameraWidget],
    active_indexes: set[int],
    failed_indexes: dict[int, float],
) -> None:
    """Log a health summary of all cameras."""
    online = 0
    for w in camera_widgets:
        if getattr(w, "_latest_frame", None) is not None:
            online += 1
    logging.info(
        "Health cameras online=%d/%d placeholders=%d active=%d failed=%d",
        online,
        len(camera_widgets),
        len(placeholder_slots),
        len(active_indexes),
        len(failed_indexes),
    )
    write_watchdog_heartbeat()
