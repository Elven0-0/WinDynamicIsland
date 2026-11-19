"""
WinDynamicIsland — A sleek, feature-rich Dynamic Island application for Windows 10/11.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import random
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from PyQt6.QtCore import (
        QEasingCurve,
        QMimeData,
        QPoint,
        QPropertyAnimation,
        QRect,
        Qt,
        QTimer,
        QUrl,
        pyqtSignal,
        QObject,
    )
    from PyQt6.QtGui import (
        QAction,
        QColor,
        QDrag,
        QFont,
        QIcon,
        QImage,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
        QPixmap,
    )
    from PyQt6.QtWidgets import (
        QApplication,
        QCheckBox,
        QFrame,
        QHBoxLayout,
        QLabel,
        QMenu,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSlider,
        QStackedWidget,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    print("PyQt6 is required. Install with: pip install -r requirements.txt")
    sys.exit(1)

IS_WINDOWS = sys.platform == "win32"

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore

try:
    import screen_brightness_control as sbc
except ImportError:
    sbc = None  # type: ignore

PYCAW_AVAILABLE = False
if IS_WINDOWS:
    try:
        from comtypes import CLSCTX_ALL
        from ctypes import cast, POINTER
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        PYCAW_AVAILABLE = True
    except Exception:
        PYCAW_AVAILABLE = False

WINRT_MEDIA_AVAILABLE = False
GlobalSystemMediaTransportControlsSessionManager = None
DataReader = None
if IS_WINDOWS:
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager,
        )
        from winrt.windows.storage.streams import DataReader

        WINRT_MEDIA_AVAILABLE = True
    except Exception:
        try:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager,
            )
            from winsdk.windows.storage.streams import DataReader

            WINRT_MEDIA_AVAILABLE = True
        except Exception:
            WINRT_MEDIA_AVAILABLE = False

APP_NAME = "WinDynamicIsland"

ACCENT_THEMES = {
    "Cyan": "#38BDF8",
    "Purple": "#A78BFA",
    "Emerald": "#34D399",
    "Amber": "#FBBF24",
    "Rose": "#FB7185",
}

IDLE_WIDTH = 220
IDLE_HEIGHT = 36
HUD_WIDTH = 310
HUD_HEIGHT = 56
EXPANDED_WIDTH = 440
EXPANDED_HEIGHT = 228


@dataclass
class AppConfig:
    top_margin: int = 8
    auto_hide_fullscreen: bool = True
    start_with_windows: bool = False
    show_clock_in_idle: bool = True
    enable_volume_hud: bool = True
    enable_battery_hud: bool = True
    enable_media_hud: bool = True
    accent_name: str = "Cyan"

    @property
    def accent_hex(self) -> str:
        return ACCENT_THEMES.get(self.accent_name, "#38BDF8")


class ConfigManager:
    def __init__(self) -> None:
        if IS_WINDOWS:
            base = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
        else:
            base = Path.home() / ".config" / APP_NAME
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / "config.json"
        self.config = self.load()

    def load(self) -> AppConfig:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                valid_fields = {f.name for f in AppConfig.__dataclass_fields__.values()}
                filtered = {k: v for k, v in data.items() if k in valid_fields}
                return AppConfig(**filtered)
            except Exception:
                pass
        return AppConfig()

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.config.__dict__, indent=2), encoding="utf-8")
        except Exception:
            pass

    def set_windows_startup(self, enable: bool) -> bool:
        self.config.start_with_windows = enable
        self.save()
        if not IS_WINDOWS:
            return False
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                if enable:
                    if getattr(sys, "frozen", False):
                        startup_cmd = f'"{sys.executable}"'
                    else:
                        python_exe = sys.executable
                        if python_exe.lower().endswith("python.exe"):
                            pythonw = python_exe[:-10] + "pythonw.exe"
                            if os.path.exists(pythonw):
                                python_exe = pythonw
                        script_path = os.path.abspath(__file__)
                        startup_cmd = f'"{python_exe}" "{script_path}"'
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, startup_cmd)
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
            return True
        except Exception:
            return False


@dataclass
class MediaState:
    title: str = "Nothing Playing"
    artist: str = "Play audio in any app"
    album: str = ""
    is_playing: bool = False
    has_session: bool = False
    thumbnail_bytes: Optional[bytes] = None


@dataclass
class SystemStats:
    cpu_percent: float = 0.0
    ram_percent: float = 0.0
    battery_percent: Optional[int] = None
    battery_plugged: Optional[bool] = None
    battery_secs_left: Optional[int] = None
    net_down_kbps: float = 0.0
    net_up_kbps: float = 0.0
    volume_percent: Optional[int] = None
    volume_muted: bool = False
    brightness_percent: Optional[int] = None


@dataclass
class ShelfFileItem:
    path: str
    name: str
    size_label: str


def format_file_size(path: str) -> str:
    try:
        if os.path.isdir(path):
            return "Folder"
        size = os.path.getsize(path)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024.0:
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"
    except OSError:
        return "Item"


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


class WindowsBridge:
    VK_VOLUME_MUTE = 0xAD
    VK_VOLUME_DOWN = 0xAE
    VK_VOLUME_UP = 0xAF
    VK_MEDIA_NEXT_TRACK = 0xB0
    VK_MEDIA_PREV_TRACK = 0xB1
    VK_MEDIA_PLAY_PAUSE = 0xB3

    def __init__(self) -> None:
        self._volume_interface = None
        self._init_pycaw()
        self._last_net_io = psutil.net_io_counters() if psutil else None
        self._last_net_time = time.monotonic()

    def _init_pycaw(self) -> None:
        if not PYCAW_AVAILABLE:
            return
        try:
            import comtypes
            comtypes.CoInitialize()
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._volume_interface = cast(interface, POINTER(IAudioEndpointVolume))
        except Exception:
            self._volume_interface = None

    def send_virtual_key(self, vk_code: int) -> None:
        if not IS_WINDOWS:
            return
        try:
            user32 = ctypes.windll.user32
            user32.keybd_event(vk_code, 0, 0, 0)
            user32.keybd_event(vk_code, 0, 2, 0)
        except Exception:
            pass

    def media_play_pause(self) -> None:
        self.send_virtual_key(self.VK_MEDIA_PLAY_PAUSE)

    def media_next(self) -> None:
        self.send_virtual_key(self.VK_MEDIA_NEXT_TRACK)

    def media_prev(self) -> None:
        self.send_virtual_key(self.VK_MEDIA_PREV_TRACK)

    def toggle_mute(self) -> None:
        if self._volume_interface is not None:
            try:
                muted = bool(self._volume_interface.GetMute())
                self._volume_interface.SetMute(0 if muted else 1, None)
                return
            except Exception:
                pass
        self.send_virtual_key(self.VK_VOLUME_MUTE)

    def get_volume(self) -> tuple[Optional[int], bool]:
        if self._volume_interface is not None:
            try:
                level = int(round(self._volume_interface.GetMasterVolumeLevelScalar() * 100))
                muted = bool(self._volume_interface.GetMute())
                return level, muted
            except Exception:
                pass
        return None, False

    def set_volume(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        if self._volume_interface is not None:
            try:
                self._volume_interface.SetMasterVolumeLevelScalar(percent / 100.0, None)
                if percent > 0:
                    self._volume_interface.SetMute(0, None)
            except Exception:
                pass

    def get_brightness(self) -> Optional[int]:
        if sbc is None:
            return None
        try:
            vals = sbc.get_brightness(display=0)
            if isinstance(vals, list) and vals:
                return int(vals[0])
            if isinstance(vals, (int, float)):
                return int(vals)
        except Exception:
            pass
        return None

    def set_brightness(self, percent: int) -> None:
        if sbc is None:
            return
        percent = max(5, min(100, int(percent)))
        try:
            sbc.set_brightness(percent, display=0)
        except Exception:
            pass

    def lock_workstation(self) -> None:
        if IS_WINDOWS:
            try:
                ctypes.windll.user32.LockWorkStation()
            except Exception:
                pass

    def is_fullscreen_app_active(self, own_hwnd: int = 0) -> bool:
        if not IS_WINDOWS:
            return False
        try:
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            state = ctypes.c_int(0)
            if shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
                if state.value in (2, 3, 4):
                    return True
            hwnd = user32.GetForegroundWindow()
            if not hwnd or hwnd == own_hwnd:
                return False
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            if class_buf.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
                return False

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            win_rect = RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(win_rect)):
                return False
            screen_w = user32.GetSystemMetrics(0)
            screen_h = user32.GetSystemMetrics(1)
            return (win_rect.right - win_rect.left) >= screen_w and (win_rect.bottom - win_rect.top) >= screen_h and win_rect.left <= 0 and win_rect.top <= 0
        except Exception:
            return False

    def poll_system_stats(self) -> SystemStats:
        stats = SystemStats()
        if psutil is not None:
            try:
                stats.cpu_percent = psutil.cpu_percent(interval=None)
                stats.ram_percent = psutil.virtual_memory().percent
                batt = psutil.sensors_battery()
                if batt is not None:
                    stats.battery_percent = int(round(batt.percent))
                    stats.battery_plugged = bool(batt.power_plugged)
                    stats.battery_secs_left = batt.secsleft if batt.secsleft not in (-1, -2) else None
                now = time.monotonic()
                dt = max(0.1, now - self._last_net_time)
                cur_io = psutil.net_io_counters()
                if self._last_net_io is not None and cur_io is not None:
                    stats.net_down_kbps = max(0.0, (cur_io.bytes_recv - self._last_net_io.bytes_recv) / 1024.0 / dt)
                    stats.net_up_kbps = max(0.0, (cur_io.bytes_sent - self._last_net_io.bytes_sent) / 1024.0 / dt)
                self._last_net_io = cur_io
                self._last_net_time = now
            except Exception:
                pass
        vol, muted = self.get_volume()
        stats.volume_percent = vol
        stats.volume_muted = muted
        stats.brightness_percent = self.get_brightness()
        return stats


class MediaPoller(QObject):
    media_updated = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_thumb_key = ""
        self._cached_thumb: Optional[bytes] = None

    def start(self) -> None:
        if not WINRT_MEDIA_AVAILABLE or self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._poll_async())
        except Exception:
            pass
        finally:
            loop.close()

    async def _poll_async(self) -> None:
        while self._running:
            try:
                manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
                session = manager.get_current_session()
                if session is None:
                    self.media_updated.emit(MediaState())
                else:
                    info = await session.try_get_media_properties_async()
                    playback = session.get_playback_info()
                    is_playing = int(playback.playback_status) == 4
                    title = (info.title or "").strip() or "Unknown Track"
                    artist = (info.artist or "").strip() or "Unknown Artist"
                    album = (info.album_title or "").strip()
                    thumb_key = f"{title}|{artist}|{album}"
                    thumb_bytes = self._cached_thumb
                    if thumb_key != self._last_thumb_key:
                        thumb_bytes = await self._read_thumbnail(info.thumbnail)
                        self._last_thumb_key = thumb_key
                        self._cached_thumb = thumb_bytes
                    self.media_updated.emit(MediaState(title=title, artist=artist, album=album, is_playing=is_playing, has_session=True, thumbnail_bytes=thumb_bytes))
            except Exception:
                pass
            await asyncio.sleep(1.0)

    async def _read_thumbnail(self, thumb_ref: Any) -> Optional[bytes]:
        if thumb_ref is None or DataReader is None:
            return None
        try:
            stream = await thumb_ref.open_read_async()
            size = int(stream.size)
            if size <= 0 or size > 5_000_000:
                return None
            reader = DataReader(stream)
            await reader.load_async(size)
            buf = bytearray(size)
            try:
                reader.read_bytes(buf)
                return bytes(buf)
            except TypeError:
                return bytes(reader.read_bytes(size))
        except Exception:
            return None


class AudioVisualizerWidget(QWidget):
    def __init__(self, accent_hex: str = "#38BDF8", bar_count: int = 5, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._accent = QColor(accent_hex)
        self._bar_count = bar_count
        self._active = False
        self._levels = [0.25] * bar_count
        self._targets = [0.25] * bar_count
        self.setFixedSize(bar_count * 5 + 2, 16)
        self._timer = QTimer(self)
        self._timer.setInterval(75)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_accent(self, accent_hex: str) -> None:
        self._accent = QColor(accent_hex)
        self.update()

    def set_active(self, active: bool) -> None:
        self._active = active
        if not active:
            self._targets = [0.2] * self._bar_count
        self.update()

    def _tick(self) -> None:
        if self._active:
            for i in range(self._bar_count):
                if random.random() < 0.65:
                    self._targets[i] = random.uniform(0.25, 1.0)
        for i in range(self._bar_count):
            self._levels[i] += (self._targets[i] - self._levels[i]) * 0.45
        self.update()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self.height()
        bar_w, gap, x = 3, 2, 1
        for i, lvl in enumerate(self._levels):
            bar_h = max(3, int(lvl * (h - 2)))
            y = (h - bar_h) // 2
            color = QColor(self._accent) if self._active else QColor(120, 125, 138)
            if self._active:
                color.setAlpha(200 + int(55 * (i / max(1, self._bar_count - 1))))
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(x, y, bar_w, bar_h, 1.5, 1.5)
            x += bar_w + gap


class RoundedArtworkLabel(QLabel):
    def __init__(self, size: int = 58, accent_hex: str = "#38BDF8", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._size = size
        self._accent = QColor(accent_hex)
        self._pixmap: Optional[QPixmap] = None
        self.setFixedSize(size, size)

    def set_accent(self, accent_hex: str) -> None:
        self._accent = QColor(accent_hex)
        self.update()

    def set_image_bytes(self, data: Optional[bytes]) -> None:
        if data:
            img = QImage.fromData(data)
            if not img.isNull():
                self._pixmap = QPixmap.fromImage(img).scaled(self._size, self._size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                self.update()
                return
        self._pixmap = None
        self.update()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self._size, self._size, 12, 12)
        painter.setClipPath(path)
        if self._pixmap is not None:
            painter.drawPixmap((self._size - self._pixmap.width()) // 2, (self._size - self._pixmap.height()) // 2, self._pixmap)
        else:
            grad = QLinearGradient(0, 0, self._size, self._size)
            grad.setColorAt(0.0, self._accent.darker(150))
            grad.setColorAt(1.0, QColor("#18181B"))
            painter.fillRect(self.rect(), grad)
            painter.setPen(QColor("#F4F4F5"))
            painter.setFont(QFont("Segoe UI Symbol", 20))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "♪")


class DraggableFileChip(QFrame):
    removed = pyqtSignal(str)

    def __init__(self, item: ShelfFileItem, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.item = item
        self._drag_start_pos: Optional[QPoint] = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setStyleSheet("DraggableFileChip { background-color: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.12); border-radius: 10px; } DraggableFileChip:hover { background-color: rgba(255,255,255,0.15); }")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 6, 4)
        layout.setSpacing(6)
        icon_lbl = QLabel("📄")
        icon_lbl.setStyleSheet("background: transparent; font-size: 12px;")
        layout.addWidget(icon_lbl)
        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        name_lbl = QLabel(item.name if len(item.name) <= 18 else item.name[:15] + "...")
        name_lbl.setToolTip(item.path)
        name_lbl.setStyleSheet("background: transparent; color: #F4F4F5; font-size: 11px; font-weight: 600;")
        size_lbl = QLabel(item.size_label)
        size_lbl.setStyleSheet("background: transparent; color: #A1A1AA; font-size: 9px;")
        text_col.addWidget(name_lbl)
        text_col.addWidget(size_lbl)
        layout.addLayout(text_col)
        rm_btn = QPushButton("×")
        rm_btn.setFixedSize(16, 16)
        rm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rm_btn.setStyleSheet("QPushButton { background: transparent; color: #A1A1AA; border: none; font-size: 13px; font-weight: bold; } QPushButton:hover { color: #FB7185; }")
        rm_btn.clicked.connect(lambda: self.removed.emit(self.item.path))
        layout.addWidget(rm_btn)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._drag_start_pos is not None and (event.buttons() & Qt.MouseButton.LeftButton) and (event.position().toPoint() - self._drag_start_pos).manhattanLength() >= 10:
            drag = QDrag(self)
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(self.item.path)])
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.CopyAction)
            self._drag_start_pos = None
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if os.path.exists(self.item.path):
            try:
                if IS_WINDOWS:
                    os.startfile(self.item.path)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", self.item.path])
            except Exception:
                pass
        super().mouseDoubleClickEvent(event)


class DynamicIslandWindow(QWidget):
    STATE_IDLE = "idle"
    STATE_HUD = "hud"
    STATE_EXPANDED = "expanded"

    def __init__(self, config_mgr: ConfigManager) -> None:
        super().__init__()
        self.config_mgr = config_mgr
        self.cfg = config_mgr.config
        self.bridge = WindowsBridge()
        self._state = self.STATE_IDLE
        self._pinned = False
        self._hidden_for_fullscreen = False
        self._media = MediaState()
        self._stats = SystemStats()
        self._last_volume: Optional[int] = None
        self._last_muted: Optional[bool] = None
        self._last_brightness: Optional[int] = None
        self._last_plugged: Optional[bool] = None
        self._low_batt_notified = False
        self._last_media_title = ""
        self._timer_mode = "countdown"
        self._timer_total_secs = 25 * 60
        self._timer_secs = 25 * 60
        self._timer_running = False
        self._shelf_files: list[ShelfFileItem] = []
        self._clipboard_history: list[str] = []

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAcceptDrops(True)

        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(320)
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack)

        self._hover_expand_timer = QTimer(self)
        self._hover_expand_timer.setSingleShot(True)
        self._hover_expand_timer.setInterval(140)
        self._hover_expand_timer.timeout.connect(self.expand)

        self._hover_collapse_timer = QTimer(self)
        self._hover_collapse_timer.setSingleShot(True)
        self._hover_collapse_timer.setInterval(480)
        self._hover_collapse_timer.timeout.connect(self._collapse_if_unpinned)

        self._hud_dismiss_timer = QTimer(self)
        self._hud_dismiss_timer.setSingleShot(True)
        self._hud_dismiss_timer.setInterval(2300)
        self._hud_dismiss_timer.timeout.connect(self._dismiss_hud)

        self._build_ui()
        self._apply_theme()
        self.setGeometry(self._target_rect(IDLE_WIDTH, IDLE_HEIGHT))

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._on_one_second_tick)
        self._clock_timer.start()

        self._system_timer = QTimer(self)
        self._system_timer.setInterval(900)
        self._system_timer.timeout.connect(self._poll_system)
        self._system_timer.start()

        self._fullscreen_timer = QTimer(self)
        self._fullscreen_timer.setInterval(1500)
        self._fullscreen_timer.timeout.connect(self._check_fullscreen)
        self._fullscreen_timer.start()

        cb = QApplication.clipboard()
        if cb is not None:
            cb.dataChanged.connect(self._on_clipboard_changed)

        self._media_poller = MediaPoller()
        self._media_poller.media_updated.connect(self._on_media_updated)
        self._media_poller.start()
        self._poll_system(initial=True)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        radius = min(rect.height() / 2.0, 26.0)
        path = QPainterPath()
        path.addRoundedRect(rect.x(), rect.y(), rect.width(), rect.height(), radius, radius)
        painter.fillPath(path, QColor(10, 10, 13, 246))
        pen = QPen(QColor(255, 255, 255, 34))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawPath(path)

    def _target_rect(self, width: int, height: int) -> QRect:
        screen = QApplication.primaryScreen()
        geo = screen.geometry() if screen else QRect(0, 0, 1920, 1080)
        return QRect(geo.x() + (geo.width() - width) // 2, geo.y() + self.cfg.top_margin, width, height)

    def _animate_to(self, width: int, height: int, easing: QEasingCurve.Type = QEasingCurve.Type.OutBack) -> None:
        self._anim.stop()
        self._anim.setEasingCurve(easing)
        self._anim.setStartValue(self.geometry())
        self._anim.setEndValue(self._target_rect(width, height))
        self._anim.start()

    def _build_ui(self) -> None:
        self.root_layout = QVBoxLayout(self)
        self.root_layout.setContentsMargins(12, 4, 12, 6)
        self.root_layout.setSpacing(0)
        self.stack = QStackedWidget(self)
        self.root_layout.addWidget(self.stack)
        self.idle_widget = self._create_idle_view()
        self.hud_widget = self._create_hud_view()
        self.expanded_widget = self._create_expanded_view()
        self.stack.addWidget(self.idle_widget)
        self.stack.addWidget(self.hud_widget)
        self.stack.addWidget(self.expanded_widget)
        self.stack.setCurrentIndex(0)

    def _create_idle_view(self) -> QWidget:
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(8)
        self.idle_visualizer = AudioVisualizerWidget(self.cfg.accent_hex, bar_count=4)
        self.idle_visualizer.hide()
        layout.addWidget(self.idle_visualizer)
        self.idle_left_icon = QLabel("●")
        self.idle_left_icon.setStyleSheet(f"color: {self.cfg.accent_hex}; font-size: 9px;")
        layout.addWidget(self.idle_left_icon)
        self.idle_center_lbl = QLabel(time.strftime("%H:%M"))
        self.idle_center_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.idle_center_lbl.setStyleSheet("color: #F4F4F5; font-family: 'Segoe UI'; font-size: 12px; font-weight: 600;")
        layout.addWidget(self.idle_center_lbl, 1)
        self.idle_right_lbl = QLabel("")
        self.idle_right_lbl.setStyleSheet("color: #A1A1AA; font-family: 'Segoe UI'; font-size: 11px; font-weight: 600;")
        layout.addWidget(self.idle_right_lbl)
        return w

    def _create_hud_view(self) -> QWidget:
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(10)
        self.hud_icon_lbl = QLabel("🔊")
        self.hud_icon_lbl.setStyleSheet("font-size: 18px; color: #F4F4F5;")
        layout.addWidget(self.hud_icon_lbl)
        mid = QVBoxLayout()
        mid.setSpacing(3)
        self.hud_title_lbl = QLabel("Volume")
        self.hud_title_lbl.setStyleSheet("color: #F4F4F5; font-size: 12px; font-weight: 600;")
        self.hud_sub_lbl = QLabel("")
        self.hud_sub_lbl.setStyleSheet("color: #A1A1AA; font-size: 10px;")
        self.hud_bar = QProgressBar()
        self.hud_bar.setRange(0, 100)
        self.hud_bar.setTextVisible(False)
        self.hud_bar.setFixedHeight(6)
        mid.addWidget(self.hud_title_lbl)
        mid.addWidget(self.hud_sub_lbl)
        mid.addWidget(self.hud_bar)
        layout.addLayout(mid, 1)
        self.hud_val_lbl = QLabel("60%")
        self.hud_val_lbl.setStyleSheet("color: #F4F4F5; font-size: 12px; font-weight: 700;")
        layout.addWidget(self.hud_val_lbl)
        return w

    def _create_expanded_view(self) -> QWidget:
        w = QWidget()
        main = QVBoxLayout(w)
        main.setContentsMargins(2, 4, 2, 2)
        main.setSpacing(8)
        nav = QHBoxLayout()
        nav.setSpacing(4)
        self.tab_buttons: list[QPushButton] = []
        for label, idx in [("♪ Media", 0), ("⏱ Timer", 1), ("⚡ System", 2), ("📁 Shelf", 3), ("⚙", 4)]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(26)
            btn.clicked.connect(lambda checked, i=idx: self._switch_tab(i))
            self.tab_buttons.append(btn)
            nav.addWidget(btn)
        nav.addStretch(1)
        self.pin_btn = QPushButton("📌")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setFixedSize(26, 26)
        self.pin_btn.setToolTip("Pin Island Open")
        self.pin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pin_btn.clicked.connect(self._toggle_pin)
        nav.addWidget(self.pin_btn)
        main.addLayout(nav)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_media_tab())
        self.pages.addWidget(self._build_timer_tab())
        self.pages.addWidget(self._build_system_tab())
        self.pages.addWidget(self._build_shelf_tab())
        self.pages.addWidget(self._build_settings_tab())
        main.addWidget(self.pages, 1)
        self._switch_tab(0)
        return w

    def _build_media_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.album_art = RoundedArtworkLabel(58, self.cfg.accent_hex)
        top.addWidget(self.album_art)
        info = QVBoxLayout()
        info.setSpacing(2)
        self.media_title_lbl = QLabel("Nothing Playing")
        self.media_title_lbl.setStyleSheet("color: #FFFFFF; font-size: 14px; font-weight: 700;")
        self.media_artist_lbl = QLabel("Play audio in Spotify, Chrome, VLC...")
        self.media_artist_lbl.setStyleSheet("color: #A1A1AA; font-size: 11px;")
        info.addWidget(self.media_title_lbl)
        info.addWidget(self.media_artist_lbl)
        info.addStretch(1)
        top.addLayout(info, 1)
        self.media_visualizer = AudioVisualizerWidget(self.cfg.accent_hex, bar_count=5)
        top.addWidget(self.media_visualizer, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)
        controls = QHBoxLayout()
        controls.setSpacing(12)
        controls.addStretch(1)
        self.btn_prev = self._icon_btn("⏮", self.bridge.media_prev, size=34)
        self.btn_play = self._icon_btn("⏯", self.bridge.media_play_pause, size=40, primary=True)
        self.btn_next = self._icon_btn("⏭", self.bridge.media_next, size=34)
        self.btn_mute = self._icon_btn("🔇", self.bridge.toggle_mute, size=30)
        controls.addWidget(self.btn_prev)
        controls.addWidget(self.btn_play)
        controls.addWidget(self.btn_next)
        controls.addStretch(1)
        controls.addWidget(self.btn_mute)
        layout.addLayout(controls)
        vol_row = QHBoxLayout()
        vol_row.setSpacing(8)
        vol_lbl = QLabel("🔊")
        vol_lbl.setStyleSheet("color: #A1A1AA; font-size: 12px;")
        self.media_vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.media_vol_slider.setRange(0, 100)
        self.media_vol_slider.setValue(50)
        self.media_vol_slider.valueChanged.connect(self._on_volume_slider_changed)
        self.media_vol_val = QLabel("50%")
        self.media_vol_val.setFixedWidth(34)
        self.media_vol_val.setStyleSheet("color: #D4D4D8; font-size: 11px; font-weight: 600;")
        vol_row.addWidget(vol_lbl)
        vol_row.addWidget(self.media_vol_slider, 1)
        vol_row.addWidget(self.media_vol_val)
        layout.addLayout(vol_row)
        return w

    def _build_timer_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(8)
        top = QHBoxLayout()
        self.timer_display = QLabel(format_duration(self._timer_secs))
        self.timer_display.setStyleSheet("color: #FFFFFF; font-family: 'Consolas', 'Segoe UI'; font-size: 32px; font-weight: 700;")
        top.addWidget(self.timer_display)
        top.addStretch(1)
        self.timer_mode_btn = QPushButton("Countdown")
        self.timer_mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.timer_mode_btn.clicked.connect(self._toggle_timer_mode)
        self.timer_mode_btn.setStyleSheet(self._pill_btn_css())
        top.addWidget(self.timer_mode_btn)
        layout.addLayout(top)
        presets = QHBoxLayout()
        presets.setSpacing(6)
        for mins, label in [(5, "5m Break"), (15, "15m"), (25, "25m Focus"), (45, "45m Deep")]:
            b = QPushButton(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(self._pill_btn_css())
            b.clicked.connect(lambda checked, m=mins: self._set_timer_preset(m))
            presets.addWidget(b)
        layout.addLayout(presets)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.timer_start_btn = QPushButton("▶ Start")
        self.timer_start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.timer_start_btn.setFixedHeight(32)
        self.timer_start_btn.clicked.connect(self._toggle_timer_running)
        self.timer_reset_btn = QPushButton("↺ Reset")
        self.timer_reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.timer_reset_btn.setFixedHeight(32)
        self.timer_reset_btn.setStyleSheet(self._pill_btn_css())
        self.timer_reset_btn.clicked.connect(self._reset_timer)
        actions.addWidget(self.timer_start_btn, 2)
        actions.addWidget(self.timer_reset_btn, 1)
        layout.addLayout(actions)
        return w

    def _build_system_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(7)
        self.cpu_bar, self.cpu_val = self._metric_row(layout, "CPU")
        self.ram_bar, self.ram_val = self._metric_row(layout, "RAM")
        self.bat_bar, self.bat_val = self._metric_row(layout, "BAT")
        net_row = QHBoxLayout()
        self.net_lbl = QLabel("↓ 0 KB/s   ↑ 0 KB/s")
        self.net_lbl.setStyleSheet("color: #A1A1AA; font-size: 11px; font-weight: 600;")
        net_row.addWidget(self.net_lbl)
        net_row.addStretch(1)
        lock_btn = QPushButton("🔒 Lock PC")
        lock_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        lock_btn.setStyleSheet(self._pill_btn_css())
        lock_btn.clicked.connect(self.bridge.lock_workstation)
        net_row.addWidget(lock_btn)
        layout.addLayout(net_row)
        br_row = QHBoxLayout()
        br_row.setSpacing(8)
        br_lbl = QLabel("☀")
        br_lbl.setStyleSheet("color: #FBBF24; font-size: 13px;")
        self.bright_slider = QSlider(Qt.Orientation.Horizontal)
        self.bright_slider.setRange(5, 100)
        self.bright_slider.setValue(80)
        self.bright_slider.valueChanged.connect(self._on_brightness_slider_changed)
        self.bright_val = QLabel("--%")
        self.bright_val.setFixedWidth(34)
        self.bright_val.setStyleSheet("color: #D4D4D8; font-size: 11px; font-weight: 600;")
        br_row.addWidget(br_lbl)
        br_row.addWidget(self.bright_slider, 1)
        br_row.addWidget(self.bright_val)
        layout.addLayout(br_row)
        return w

    def _metric_row(self, parent_layout: QVBoxLayout, label: str) -> tuple[QProgressBar, QLabel]:
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(label)
        lbl.setFixedWidth(30)
        lbl.setStyleSheet("color: #A1A1AA; font-size: 11px; font-weight: 700;")
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setFixedHeight(7)
        val = QLabel("0%")
        val.setFixedWidth(68)
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val.setStyleSheet("color: #F4F4F5; font-size: 11px; font-weight: 600;")
        row.addWidget(lbl)
        row.addWidget(bar, 1)
        row.addWidget(val)
        parent_layout.addLayout(row)
        return bar, val

    def _build_shelf_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)
        header = QHBoxLayout()
        lbl = QLabel("File Shelf (Drop files here, drag out anywhere)")
        lbl.setStyleSheet("color: #D4D4D8; font-size: 11px; font-weight: 600;")
        header.addWidget(lbl)
        header.addStretch(1)
        clear_btn = QPushButton("Clear")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setStyleSheet(self._pill_btn_css())
        clear_btn.clicked.connect(self._clear_shelf)
        header.addWidget(clear_btn)
        layout.addLayout(header)
        self.shelf_scroll = QScrollArea()
        self.shelf_scroll.setWidgetResizable(True)
        self.shelf_scroll.setFixedHeight(56)
        self.shelf_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.shelf_container = QWidget()
        self.shelf_container.setStyleSheet("background: transparent;")
        self.shelf_layout = QHBoxLayout(self.shelf_container)
        self.shelf_layout.setContentsMargins(0, 2, 0, 2)
        self.shelf_layout.setSpacing(6)
        self.shelf_empty_lbl = QLabel("⬇  Drag & drop files onto the Island to stash them")
        self.shelf_empty_lbl.setStyleSheet("color: #71717A; font-size: 11px; border: 1px dashed rgba(255,255,255,0.16); border-radius: 10px; padding: 10px;")
        self.shelf_empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.shelf_layout.addWidget(self.shelf_empty_lbl)
        self.shelf_scroll.setWidget(self.shelf_container)
        layout.addWidget(self.shelf_scroll)
        cb_lbl = QLabel("Recent Clipboard (Click to re-copy)")
        cb_lbl.setStyleSheet("color: #A1A1AA; font-size: 10px; font-weight: 600;")
        layout.addWidget(cb_lbl)
        self.cb_row = QHBoxLayout()
        self.cb_row.setSpacing(6)
        self.cb_empty_lbl = QLabel("Copy any text to see history here")
        self.cb_empty_lbl.setStyleSheet("color: #71717A; font-size: 10px;")
        self.cb_row.addWidget(self.cb_empty_lbl)
        layout.addLayout(self.cb_row)
        return w

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)
        row1 = QHBoxLayout()
        self.chk_fullscreen = QCheckBox("Auto-hide in Fullscreen")
        self.chk_fullscreen.setChecked(self.cfg.auto_hide_fullscreen)
        self.chk_fullscreen.toggled.connect(self._on_settings_changed)
        self.chk_startup = QCheckBox("Start with Windows")
        self.chk_startup.setChecked(self.cfg.start_with_windows)
        self.chk_startup.toggled.connect(self._on_startup_toggled)
        row1.addWidget(self.chk_fullscreen)
        row1.addWidget(self.chk_startup)
        layout.addLayout(row1)
        row2 = QHBoxLayout()
        self.chk_vol_hud = QCheckBox("Volume/Bright HUD")
        self.chk_vol_hud.setChecked(self.cfg.enable_volume_hud)
        self.chk_vol_hud.toggled.connect(self._on_settings_changed)
        self.chk_batt_hud = QCheckBox("Battery/Power HUD")
        self.chk_batt_hud.setChecked(self.cfg.enable_battery_hud)
        self.chk_batt_hud.toggled.connect(self._on_settings_changed)
        row2.addWidget(self.chk_vol_hud)
        row2.addWidget(self.chk_batt_hud)
        layout.addLayout(row2)
        accent_row = QHBoxLayout()
        accent_lbl = QLabel("Accent:")
        accent_lbl.setStyleSheet("color: #A1A1AA; font-size: 11px; font-weight: 600;")
        accent_row.addWidget(accent_lbl)
        for name, hex_code in ACCENT_THEMES.items():
            b = QPushButton("")
            b.setFixedSize(20, 20)
            b.setToolTip(name)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"QPushButton {{ background-color: {hex_code}; border-radius: 10px; border: 2px solid rgba(255,255,255,0.25); }} QPushButton:hover {{ border: 2px solid #FFFFFF; }}")
            b.clicked.connect(lambda checked, n=name: self._set_accent(n))
            accent_row.addWidget(b)
        accent_row.addStretch(1)
        quit_btn = QPushButton("Quit App")
        quit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        quit_btn.setStyleSheet("QPushButton { background-color: rgba(251,113,133,0.18); color: #FB7185; border: 1px solid rgba(251,113,133,0.35); border-radius: 8px; padding: 4px 10px; font-size: 11px; font-weight: 600; } QPushButton:hover { background-color: rgba(251,113,133,0.32); }")
        quit_btn.clicked.connect(QApplication.quit)
        accent_row.addWidget(quit_btn)
        layout.addLayout(accent_row)
        return w

    def _icon_btn(self, text: str, slot: Callable[[], None], size: int = 34, primary: bool = False) -> QPushButton:
        btn = QPushButton(text)
        btn.setFixedSize(size, size)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(slot)
        btn.setProperty("primary_icon", primary)
        return btn

    def _pill_btn_css(self) -> str:
        return "QPushButton { background-color: rgba(255,255,255,0.08); color: #E4E4E7; border: 1px solid rgba(255,255,255,0.10); border-radius: 8px; padding: 4px 10px; font-size: 11px; font-weight: 600; } QPushButton:hover { background-color: rgba(255,255,255,0.16); }"

    def _apply_theme(self) -> None:
        accent = self.cfg.accent_hex
        self.idle_visualizer.set_accent(accent)
        self.media_visualizer.set_accent(accent)
        self.album_art.set_accent(accent)
        self.idle_left_icon.setStyleSheet(f"color: {accent}; font-size: 9px;")
        bar_css = f"QProgressBar {{ background-color: rgba(255,255,255,0.10); border: none; border-radius: 3px; }} QProgressBar::chunk {{ background-color: {accent}; border-radius: 3px; }}"
        for bar in (self.hud_bar, self.cpu_bar, self.ram_bar, self.bat_bar):
            bar.setStyleSheet(bar_css)
        slider_css = f"QSlider::groove:horizontal {{ height: 5px; background: rgba(255,255,255,0.14); border-radius: 2px; }} QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }} QSlider::handle:horizontal {{ background: #FFFFFF; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }}"
        self.media_vol_slider.setStyleSheet(slider_css)
        self.bright_slider.setStyleSheet(slider_css)
        chk_css = f"QCheckBox {{ color: #E4E4E7; font-size: 11px; spacing: 6px; }} QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 4px; border: 1px solid rgba(255,255,255,0.28); background: rgba(255,255,255,0.06); }} QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; }}"
        for chk in (self.chk_fullscreen, self.chk_startup, self.chk_vol_hud, self.chk_batt_hud):
            chk.setStyleSheet(chk_css)
        self.timer_start_btn.setStyleSheet(f"QPushButton {{ background-color: {accent}; color: #09090B; border: none; border-radius: 10px; font-size: 12px; font-weight: 700; }} QPushButton:hover {{ background-color: #FFFFFF; }}")
        for btn in (self.btn_prev, self.btn_play, self.btn_next, self.btn_mute):
            r = btn.width() // 2
            if bool(btn.property("primary_icon")):
                btn.setStyleSheet(f"QPushButton {{ background-color: {accent}; color: #09090B; border: none; border-radius: {r}px; font-size: 16px; font-weight: 700; }} QPushButton:hover {{ background-color: #FFFFFF; }}")
            else:
                btn.setStyleSheet(f"QPushButton {{ background-color: rgba(255,255,255,0.09); color: #F4F4F5; border: none; border-radius: {r}px; font-size: 13px; }} QPushButton:hover {{ background-color: rgba(255,255,255,0.18); }}")
        self._style_tab_buttons()
        self._style_pin_button()

    def _style_tab_buttons(self) -> None:
        accent = self.cfg.accent_hex
        current = self.pages.currentIndex()
        for idx, btn in enumerate(self.tab_buttons):
            btn.setChecked(idx == current)
            if idx == current:
                btn.setStyleSheet(f"QPushButton {{ background-color: {accent}; color: #09090B; border: none; border-radius: 11px; padding: 2px 10px; font-size: 11px; font-weight: 700; }}")
            else:
                btn.setStyleSheet("QPushButton { background-color: rgba(255,255,255,0.06); color: #A1A1AA; border: none; border-radius: 11px; padding: 2px 10px; font-size: 11px; font-weight: 600; } QPushButton:hover { background-color: rgba(255,255,255,0.14); color: #FFFFFF; }")

    def _style_pin_button(self) -> None:
        accent = self.cfg.accent_hex
        if self._pinned:
            self.pin_btn.setStyleSheet(f"QPushButton {{ background-color: {accent}; color: #09090B; border-radius: 13px; border: none; font-size: 12px; }}")
        else:
            self.pin_btn.setStyleSheet("QPushButton { background-color: rgba(255,255,255,0.08); color: #A1A1AA; border-radius: 13px; border: none; font-size: 12px; } QPushButton:hover { background-color: rgba(255,255,255,0.16); }")

    def expand(self) -> None:
        self._hover_collapse_timer.stop()
        self._hud_dismiss_timer.stop()
        if self._state == self.STATE_EXPANDED:
            return
        self._state = self.STATE_EXPANDED
        self.stack.setCurrentIndex(2)
        self._animate_to(EXPANDED_WIDTH, EXPANDED_HEIGHT, QEasingCurve.Type.OutBack)

    def collapse(self) -> None:
        self._hover_expand_timer.stop()
        self._hud_dismiss_timer.stop()
        if self._state == self.STATE_IDLE:
            return
        self._state = self.STATE_IDLE
        self.stack.setCurrentIndex(0)
        self._animate_to(IDLE_WIDTH, IDLE_HEIGHT, QEasingCurve.Type.OutCubic)

    def _collapse_if_unpinned(self) -> None:
        if not self._pinned and not self.underMouse():
            self.collapse()

    def show_hud(self, icon: str, title: str, subtitle: str = "", value_percent: Optional[int] = None, value_text: str = "", duration_ms: int = 2300) -> None:
        if self._hidden_for_fullscreen or self._state == self.STATE_EXPANDED:
            return
        self._state = self.STATE_HUD
        self.hud_icon_lbl.setText(icon)
        self.hud_title_lbl.setText(title)
        self.hud_sub_lbl.setText(subtitle)
        self.hud_sub_lbl.setVisible(bool(subtitle))
        if value_percent is not None:
            self.hud_bar.setValue(max(0, min(100, value_percent)))
            self.hud_bar.show()
            self.hud_val_lbl.setText(value_text or f"{value_percent}%")
            self.hud_val_lbl.show()
        else:
            self.hud_bar.hide()
            self.hud_val_lbl.setText(value_text)
            self.hud_val_lbl.setVisible(bool(value_text))
        self.stack.setCurrentIndex(1)
        self._animate_to(HUD_WIDTH, HUD_HEIGHT, QEasingCurve.Type.OutBack)
        self._hud_dismiss_timer.start(duration_ms)

    def _dismiss_hud(self) -> None:
        if self._state == self.STATE_HUD and not self.underMouse():
            self.collapse()

    def enterEvent(self, event: Any) -> None:
        self._hover_collapse_timer.stop()
        if self._state != self.STATE_EXPANDED:
            self._hover_expand_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self._hover_expand_timer.stop()
        if self._state == self.STATE_EXPANDED and not self._pinned:
            self._hover_collapse_timer.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._state != self.STATE_EXPANDED:
            self.expand()
        super().mousePressEvent(event)

    def dragEnterEvent(self, event: Any) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._switch_tab(3)
            self.expand()
        else:
            event.ignore()

    def dropEvent(self, event: Any) -> None:
        if event.mimeData().hasUrls():
            added = 0
            existing = {f.path for f in self._shelf_files}
            for url in event.mimeData().urls():
                local = url.toLocalFile()
                if local and local not in existing and os.path.exists(local):
                    self._shelf_files.append(ShelfFileItem(path=local, name=os.path.basename(local.rstrip("\\/")) or local, size_label=format_file_size(local)))
                    existing.add(local)
                    added += 1
            if added:
                self._rebuild_shelf_ui()
            event.acceptProposedAction()

    def _on_one_second_tick(self) -> None:
        if self._timer_running:
            if self._timer_mode == "countdown":
                if self._timer_secs > 0:
                    self._timer_secs -= 1
                if self._timer_secs == 0:
                    self._timer_running = False
                    self.timer_start_btn.setText("▶ Start")
                    self._play_alert_beep()
                    self.show_hud("⏰", "Timer Finished!", "Time is up", value_text="DONE", duration_ms=4000)
            else:
                self._timer_secs += 1
            self.timer_display.setText(format_duration(self._timer_secs))
        self._refresh_idle_pill()

    def _refresh_idle_pill(self) -> None:
        if self._media.is_playing:
            self.idle_visualizer.set_active(True)
            self.idle_visualizer.show()
            self.idle_left_icon.hide()
        else:
            self.idle_visualizer.set_active(False)
            self.idle_visualizer.hide()
            self.idle_left_icon.show()
        if self._timer_running:
            self.idle_center_lbl.setText(f"⏱ {format_duration(self._timer_secs)}")
        elif self._media.is_playing and self._media.has_session:
            short_title = self._media.title
            self.idle_center_lbl.setText(short_title if len(short_title) <= 18 else short_title[:16] + "…")
        elif self.cfg.show_clock_in_idle:
            self.idle_center_lbl.setText(time.strftime("%I:%M %p").lstrip("0"))
        else:
            self.idle_center_lbl.setText("Dynamic Island")
        if self._stats.battery_percent is not None:
            bolt = "⚡" if self._stats.battery_plugged else ""
            self.idle_right_lbl.setText(f"{bolt}{self._stats.battery_percent}%")
        elif self._shelf_files:
            self.idle_right_lbl.setText(f"📁{len(self._shelf_files)}")
        else:
            self.idle_right_lbl.setText("")

    def _poll_system(self, initial: bool = False) -> None:
        stats = self.bridge.poll_system_stats()
        self._stats = stats
        self.cpu_bar.setValue(int(round(stats.cpu_percent)))
        self.cpu_val.setText(f"{stats.cpu_percent:.0f}%")
        self.ram_bar.setValue(int(round(stats.ram_percent)))
        self.ram_val.setText(f"{stats.ram_percent:.0f}%")
        if stats.battery_percent is not None:
            self.bat_bar.setValue(stats.battery_percent)
            self.bat_val.setText(f"{'⚡ ' if stats.battery_plugged else ''}{stats.battery_percent}%")
        else:
            self.bat_bar.setValue(100)
            self.bat_val.setText("AC Power")
        down_str = f"{stats.net_down_kbps / 1024:.1f} MB/s" if stats.net_down_kbps >= 1024 else f"{stats.net_down_kbps:.0f} KB/s"
        up_str = f"{stats.net_up_kbps / 1024:.1f} MB/s" if stats.net_up_kbps >= 1024 else f"{stats.net_up_kbps:.0f} KB/s"
        self.net_lbl.setText(f"↓ {down_str}   ↑ {up_str}")
        if stats.volume_percent is not None:
            if not self.media_vol_slider.isSliderDown():
                self.media_vol_slider.blockSignals(True)
                self.media_vol_slider.setValue(stats.volume_percent)
                self.media_vol_slider.blockSignals(False)
            self.media_vol_val.setText("Mute" if stats.volume_muted else f"{stats.volume_percent}%")
            if not initial and self.cfg.enable_volume_hud and (stats.volume_percent != self._last_volume or stats.volume_muted != self._last_muted):
                icon = "🔇" if stats.volume_muted else ("🔉" if stats.volume_percent < 50 else "🔊")
                self.show_hud(icon, "Muted" if stats.volume_muted else "Volume", value_percent=0 if stats.volume_muted else stats.volume_percent, value_text="Mute" if stats.volume_muted else f"{stats.volume_percent}%")
            self._last_volume = stats.volume_percent
            self._last_muted = stats.volume_muted
        if stats.brightness_percent is not None:
            if not self.bright_slider.isSliderDown():
                self.bright_slider.blockSignals(True)
                self.bright_slider.setValue(stats.brightness_percent)
                self.bright_slider.blockSignals(False)
            self.bright_val.setText(f"{stats.brightness_percent}%")
            if not initial and self.cfg.enable_volume_hud and self._last_brightness is not None and stats.brightness_percent != self._last_brightness:
                self.show_hud("☀", "Brightness", value_percent=stats.brightness_percent)
            self._last_brightness = stats.brightness_percent
        if stats.battery_percent is not None and stats.battery_plugged is not None:
            if not initial and self.cfg.enable_battery_hud and self._last_plugged is not None and stats.battery_plugged != self._last_plugged:
                if stats.battery_plugged:
                    self.show_hud("⚡", "Charging", f"Battery at {stats.battery_percent}%", stats.battery_percent)
                else:
                    self.show_hud("🔋", "On Battery", f"{stats.battery_percent}% remaining", stats.battery_percent)
            if not stats.battery_plugged and stats.battery_percent <= 20 and not self._low_batt_notified:
                self._low_batt_notified = True
                if self.cfg.enable_battery_hud and not initial:
                    self.show_hud("🪫", "Low Battery", "Plug in your charger soon", stats.battery_percent)
            elif stats.battery_percent > 25 or stats.battery_plugged:
                self._low_batt_notified = False
            self._last_plugged = stats.battery_plugged

    def _on_media_updated(self, media: MediaState) -> None:
        self._media = media
        title = media.title if len(media.title) <= 28 else media.title[:26] + "…"
        artist = media.artist if len(media.artist) <= 34 else media.artist[:32] + "…"
        self.media_title_lbl.setText(title)
        self.media_artist_lbl.setText(artist)
        self.album_art.set_image_bytes(media.thumbnail_bytes)
        self.media_visualizer.set_active(media.is_playing)
        self.btn_play.setText("⏸" if media.is_playing else "▶")
        if self.cfg.enable_media_hud and media.has_session and media.is_playing and media.title != self._last_media_title and self._last_media_title != "":
            self.show_hud("🎵", title, artist, duration_ms=2600)
        if media.has_session:
            self._last_media_title = media.title
        self._refresh_idle_pill()

    def _check_fullscreen(self) -> None:
        if not self.cfg.auto_hide_fullscreen:
            if self._hidden_for_fullscreen:
                self._hidden_for_fullscreen = False
                self.show()
            return
        is_fs = self.bridge.is_fullscreen_app_active(int(self.winId()) if IS_WINDOWS else 0)
        if is_fs and not self._hidden_for_fullscreen:
            self._hidden_for_fullscreen = True
            self.hide()
        elif not is_fs and self._hidden_for_fullscreen:
            self._hidden_for_fullscreen = False
            self.show()

    def _switch_tab(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self._style_tab_buttons()

    def _toggle_pin(self) -> None:
        self._pinned = self.pin_btn.isChecked()
        self._style_pin_button()

    def _on_volume_slider_changed(self, value: int) -> None:
        self.media_vol_val.setText(f"{value}%")
        self.bridge.set_volume(value)
        self._last_volume = value

    def _on_brightness_slider_changed(self, value: int) -> None:
        self.bright_val.setText(f"{value}%")
        self.bridge.set_brightness(value)
        self._last_brightness = value

    def _set_timer_preset(self, minutes: int) -> None:
        self._timer_mode = "countdown"
        self.timer_mode_btn.setText("Countdown")
        self._timer_total_secs = minutes * 60
        self._timer_secs = minutes * 60
        self._timer_running = False
        self.timer_start_btn.setText("▶ Start")
        self.timer_display.setText(format_duration(self._timer_secs))

    def _toggle_timer_mode(self) -> None:
        self._timer_running = False
        self.timer_start_btn.setText("▶ Start")
        if self._timer_mode == "countdown":
            self._timer_mode = "stopwatch"
            self.timer_mode_btn.setText("Stopwatch")
            self._timer_secs = 0
        else:
            self._timer_mode = "countdown"
            self.timer_mode_btn.setText("Countdown")
            self._timer_secs = self._timer_total_secs
        self.timer_display.setText(format_duration(self._timer_secs))

    def _toggle_timer_running(self) -> None:
        if self._timer_mode == "countdown" and self._timer_secs == 0:
            self._timer_secs = self._timer_total_secs
        self._timer_running = not self._timer_running
        self.timer_start_btn.setText("⏸ Pause" if self._timer_running else "▶ Start")
        self._refresh_idle_pill()

    def _reset_timer(self) -> None:
        self._timer_running = False
        self.timer_start_btn.setText("▶ Start")
        self._timer_secs = self._timer_total_secs if self._timer_mode == "countdown" else 0
        self.timer_display.setText(format_duration(self._timer_secs))
        self._refresh_idle_pill()

    def _play_alert_beep(self) -> None:
        if IS_WINDOWS:
            try:
                import winsound
                threading.Thread(target=lambda: (winsound.Beep(880, 180), winsound.Beep(1175, 250)), daemon=True).start()
            except Exception:
                pass

    def _rebuild_shelf_ui(self) -> None:
        while self.shelf_layout.count():
            child = self.shelf_layout.takeAt(0)
            if child.widget() is not None:
                child.widget().setParent(None)
        if not self._shelf_files:
            self.shelf_layout.addWidget(self.shelf_empty_lbl)
            return
        for item in self._shelf_files:
            chip = DraggableFileChip(item)
            chip.removed.connect(self._remove_shelf_item)
            self.shelf_layout.addWidget(chip)
        self.shelf_layout.addStretch(1)

    def _remove_shelf_item(self, path: str) -> None:
        self._shelf_files = [f for f in self._shelf_files if f.path != path]
        self._rebuild_shelf_ui()
        self._refresh_idle_pill()

    def _clear_shelf(self) -> None:
        self._shelf_files.clear()
        self._rebuild_shelf_ui()
        self._refresh_idle_pill()

    def _on_clipboard_changed(self) -> None:
        cb = QApplication.clipboard()
        if cb is None:
            return
        text = cb.text().strip()
        if not text:
            return
        if text in self._clipboard_history:
            self._clipboard_history.remove(text)
        self._clipboard_history.insert(0, text)
        self._clipboard_history = self._clipboard_history[:4]
        self._rebuild_clipboard_ui()

    def _rebuild_clipboard_ui(self) -> None:
        while self.cb_row.count():
            child = self.cb_row.takeAt(0)
            if child.widget() is not None:
                child.widget().setParent(None)
        if not self._clipboard_history:
            self.cb_row.addWidget(self.cb_empty_lbl)
            return
        for text in self._clipboard_history:
            preview = text.replace("\n", " ")
            btn = QPushButton(preview if len(preview) <= 14 else preview[:12] + "…")
            btn.setToolTip(text[:200])
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(self._pill_btn_css())
            btn.clicked.connect(lambda checked, t=text: self._copy_to_clipboard(t))
            self.cb_row.addWidget(btn)

    def _copy_to_clipboard(self, text: str) -> None:
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(text)

    def _on_settings_changed(self) -> None:
        self.cfg.auto_hide_fullscreen = self.chk_fullscreen.isChecked()
        self.cfg.enable_volume_hud = self.chk_vol_hud.isChecked()
        self.cfg.enable_battery_hud = self.chk_batt_hud.isChecked()
        self.config_mgr.save()

    def _on_startup_toggled(self, checked: bool) -> None:
        self.config_mgr.set_windows_startup(checked)

    def _set_accent(self, name: str) -> None:
        self.cfg.accent_name = name
        self.config_mgr.save()
        self._apply_theme()

    def closeEvent(self, event: Any) -> None:
        self._media_poller.stop()
        super().closeEvent(event)


def create_tray_icon(window: DynamicIslandWindow) -> QSystemTrayIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#09090B"))
    p.setPen(QPen(QColor("#38BDF8"), 3))
    p.drawRoundedRect(6, 18, 52, 28, 14, 14)
    p.setBrush(QColor("#38BDF8"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(16, 26, 12, 12)
    p.end()
    tray = QSystemTrayIcon(QIcon(pix), window)
    tray.setToolTip("WinDynamicIsland")
    menu = QMenu()
    act_toggle = QAction("Show / Hide Island", menu)
    act_toggle.triggered.connect(lambda: window.hide() if window.isVisible() else window.show())
    act_expand = QAction("Expand Control Hub", menu)
    act_expand.triggered.connect(lambda: (window.show(), window.expand()))
    act_recenter = QAction("Recenter on Primary Monitor", menu)
    act_recenter.triggered.connect(lambda: window.collapse())
    act_quit = QAction("Quit WinDynamicIsland", menu)
    act_quit.triggered.connect(QApplication.quit)
    menu.addAction(act_toggle)
    menu.addAction(act_expand)
    menu.addAction(act_recenter)
    menu.addSeparator()
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: (window.show(), window.expand()) if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()
    return tray


def main() -> None:
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    config_mgr = ConfigManager()
    window = DynamicIslandWindow(config_mgr)
    tray = create_tray_icon(window)
    window.show()
    app._tray_ref = tray  # type: ignore[attr-defined]
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
