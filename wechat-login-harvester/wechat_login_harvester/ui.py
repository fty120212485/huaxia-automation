from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WindowBounds:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class MiniProgramWindow:
    window_id: int
    owner_name: str
    name: str
    bounds: WindowBounds


@dataclass(frozen=True)
class TextObservation:
    text: str
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


def find_miniprogram_window(
    *,
    owner_name: str = "微信",
    window_name: str = "华夏会计网校",
) -> MiniProgramWindow:
    import Quartz

    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    windows = Quartz.CGWindowListCopyWindowInfo(
        options,
        Quartz.kCGNullWindowID,
    )
    for info in windows or []:
        if info.get(Quartz.kCGWindowOwnerName) != owner_name:
            continue
        if info.get(Quartz.kCGWindowName) != window_name:
            continue
        bounds = info.get(Quartz.kCGWindowBounds, {})
        return MiniProgramWindow(
            window_id=int(info.get(Quartz.kCGWindowNumber, 0)),
            owner_name=owner_name,
            name=window_name,
            bounds=WindowBounds(
                x=int(bounds.get("X", 0)),
                y=int(bounds.get("Y", 0)),
                width=int(bounds.get("Width", 0)),
                height=int(bounds.get("Height", 0)),
            ),
        )
    raise RuntimeError(f"未找到微信小程序窗口：{owner_name}/{window_name}")


def bring_wechat_frontmost() -> None:
    script = (
        'tell application "System Events" to tell process "WeChat" '
        "to set frontmost to true"
    )
    subprocess.run(
        ["osascript", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )


def capture_window(window: MiniProgramWindow, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    region = (
        f"{window.bounds.x},{window.bounds.y},"
        f"{window.bounds.width},{window.bounds.height}"
    )
    cmd = ["screencapture", "-x", "-R", region, str(path)]
    last_error = ""
    for _ in range(3):
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
            return path
        except subprocess.CalledProcessError as exc:
            last_error = (exc.stderr or "").strip() or str(exc)
            time.sleep(1)
    raise RuntimeError(f"屏幕截图失败：{last_error}")


def recognize_text(path: Path) -> list[TextObservation]:
    import Foundation
    import Quartz
    import Vision

    url = Foundation.NSURL.fileURLWithPath_(str(path))
    source = Quartz.CGImageSourceCreateWithURL(url, None)
    props = Quartz.CGImageSourceCopyPropertiesAtIndex(source, 0, None)
    width = int(props["PixelWidth"])
    height = int(props["PixelHeight"])

    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, {})
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(["zh-Hans", "en-US"])
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"OCR 失败：{error}")

    observations: list[TextObservation] = []
    for result in request.results() or []:
        candidate = result.topCandidates_(1)[0]
        box = result.boundingBox()
        observations.append(
            TextObservation(
                text=candidate.string(),
                x=box.origin.x * width,
                y=(1 - box.origin.y - box.size.height) * height,
                width=box.size.width * width,
                height=box.size.height * height,
            )
        )
    return observations


def click_screen_point(x: float, y: float) -> None:
    import Quartz

    point = Quartz.CGPointMake(float(x), float(y))
    down = Quartz.CGEventCreateMouseEvent(
        None,
        Quartz.kCGEventLeftMouseDown,
        point,
        Quartz.kCGMouseButtonLeft,
    )
    up = Quartz.CGEventCreateMouseEvent(
        None,
        Quartz.kCGEventLeftMouseUp,
        point,
        Quartz.kCGMouseButtonLeft,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.08)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def type_text(text: str) -> None:
    import Quartz

    for char in text:
        down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
        Quartz.CGEventKeyboardSetUnicodeString(down, 1, char)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)

        up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
        time.sleep(0.02)


def set_clipboard(text: str) -> None:
    import AppKit

    pasteboard = AppKit.NSPasteboard.generalPasteboard()
    pasteboard.clearContents()
    pasteboard.setString_forType_(text, AppKit.NSPasteboardTypeString)


def paste_clipboard() -> None:
    import Quartz

    source = Quartz.CGEventSourceCreate(
        Quartz.kCGEventSourceStateHIDSystemState
    )
    down = Quartz.CGEventCreateKeyboardEvent(source, 9, True)
    Quartz.CGEventSetFlags(down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    up = Quartz.CGEventCreateKeyboardEvent(source, 9, False)
    Quartz.CGEventSetFlags(up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def scroll_pixels_at_screen(x: float, y: float, pixels: int) -> None:
    import Quartz

    point = Quartz.CGPointMake(float(x), float(y))
    move = Quartz.CGEventCreateMouseEvent(
        None,
        Quartz.kCGEventMouseMoved,
        point,
        Quartz.kCGMouseButtonLeft,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    event = Quartz.CGEventCreateScrollWheelEvent(
        None,
        Quartz.kCGScrollEventUnitPixel,
        1,
        pixels,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
