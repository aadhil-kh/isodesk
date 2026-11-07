"""Window capture and set-of-mark annotation.

The usual capture ladder tries pywinauto's `capture_as_image`, then PrintWindow,
then a full-desktop grab. Phase 0 measured all three against a WinUI 3 window on a
background desktop: only `PrintWindow` with `PW_RENDERFULLCONTENT` returns real
content (0.977 non-black, 40151 colors); plain PrintWindow and BitBlt both return
a near-empty frame (0.021 non-black, 125 colors). The desktop-grab fallback is
removed outright - silently returning a blank or wrong-desktop frame is worse
than raising, because the caller is an agent that would go on to reason about it.
"""

from __future__ import annotations

import base64
import ctypes
import io
from typing import Any, Dict, List, Optional, Sequence, Tuple

import win32gui
import win32ui
from PIL import Image, ImageDraw, ImageFont

from .win32.desktop import is_minimized, restore_window, window_exists, window_rect

PW_RENDERFULLCONTENT = 2

# Tuned from the Phase 0 measurements: a real WinUI 3 frame scored 0.977/40151,
# a dead frame 0.021/125. Anything near the latter is not a usable screenshot.
_MIN_NON_BLACK_RATIO = 0.05
_MIN_DISTINCT_COLORS = 8


class CaptureError(RuntimeError):
    pass


def _bitmap_to_image(bitmap) -> Image.Image:
    info = bitmap.GetInfo()
    bits = bitmap.GetBitmapBits(True)
    return Image.frombuffer(
        "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1
    )


def image_quality(img: Image.Image) -> Dict[str, Any]:
    """Cheap heuristic for 'did this window actually render?'."""
    rgb = img.convert("RGB")
    sample = rgb.resize((min(rgb.width, 200), min(rgb.height, 200)))
    pixels = list(sample.getdata())
    non_black = sum(1 for p in pixels if p != (0, 0, 0))
    colors = sample.getcolors(maxcolors=65536) or []
    ratio = non_black / len(pixels) if pixels else 0.0
    return {
        "non_black_ratio": round(ratio, 4),
        "distinct_colors": len(colors),
        "looks_rendered": ratio >= _MIN_NON_BLACK_RATIO and len(colors) >= _MIN_DISTINCT_COLORS,
    }


def capture_window(hwnd: int, auto_restore: bool = True) -> Image.Image:
    """Capture one window via PrintWindow(PW_RENDERFULLCONTENT).

    :param auto_restore: un-minimize first. A minimized window renders nothing,
        which was a real source of silently-blank frames during the spike.
    """
    # Check this first: once the window is gone, every later symptom is a
    # misleading one. GetWindowRect on a dead hwnd quietly returns 0x0, and the
    # GDI teardown fails with "DeleteDC failed" - neither of which says "the app
    # exited", which is what actually happened.
    if not window_exists(hwnd):
        raise CaptureError(
            f"window {hwnd} no longer exists - it was closed, or the app crashed "
            "or exited. Call list_windows to see what is left on this desktop."
        )

    if auto_restore and is_minimized(hwnd):
        restore_window(hwnd)
        import time

        time.sleep(0.6)

    rect = window_rect(hwnd)
    w, h = rect["width"], rect["height"]
    if w <= 0 or h <= 0:
        raise CaptureError(f"window {hwnd} has an empty rect ({w}x{h})")

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bitmap)
    try:
        ok = ctypes.windll.user32.PrintWindow(
            hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT
        )
        if not ok:
            raise CaptureError(f"PrintWindow returned 0 for hwnd {hwnd}")
        return _bitmap_to_image(bitmap)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def activate_on_desktop(hwnd: int) -> None:
    """Bring a window to the front *of its own desktop*.

    Called only from the worker, so this raises the window within the isolated
    desktop and cannot affect the user's z-order. It matters because a WinUI 3
    window that is occluded or inactive on a background desktop may not paint,
    which shows up as a capture full of dead pixels.
    """
    import ctypes

    user32 = ctypes.windll.user32
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetActiveWindow(hwnd)


def capture_window_checked(
    hwnd: int, retries: int = 2, settle: float = 0.8
) -> Tuple[Image.Image, Dict[str, Any]]:
    """Capture, and if the frame looks dead, activate the window and retry.

    A blank frame is usually a window that has not painted yet - freshly
    launched, minimized, or sitting behind another window on the isolated
    desktop. Retrying after raising it recovers the common cases instead of
    handing the agent a black rectangle to reason about.
    """
    import time

    image = capture_window(hwnd)
    quality = image_quality(image)

    for _ in range(retries):
        if quality["looks_rendered"]:
            break
        activate_on_desktop(hwnd)
        time.sleep(settle)
        image = capture_window(hwnd)
        quality = image_quality(image)
        quality["recovered_by_activation"] = quality["looks_rendered"]

    return image, quality


# ---------------------------------------------------------------------------
# set-of-mark annotation
# ---------------------------------------------------------------------------

_PALETTE = [
    (255, 87, 51), (46, 204, 113), (52, 152, 219), (241, 196, 15),
    (155, 89, 182), (26, 188, 156), (231, 76, 60), (52, 73, 94),
]


def _font(size: int):
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def annotate(
    img: Image.Image,
    controls: Sequence[Dict[str, Any]],
    window_origin: Tuple[int, int],
    font_size: int = 15,
) -> Image.Image:
    """Draw numbered boxes over controls, the set-of-mark grounding aid.

    Control rectangles come from UIA in screen coordinates, so they are shifted
    by the window origin to land correctly on a window-relative capture.
    """
    out = img.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    font = _font(font_size)
    ox, oy = window_origin

    for index, control in enumerate(controls):
        rect = control.get("rect") or {}
        label = str(control.get("id", index))
        left = rect.get("left", 0) - ox
        top = rect.get("top", 0) - oy
        right = rect.get("right", 0) - ox
        bottom = rect.get("bottom", 0) - oy
        if right <= left or bottom <= top:
            continue

        color = _PALETTE[index % len(_PALETTE)]
        draw.rectangle([left, top, right, bottom], outline=color, width=2)

        # Label sits inside the top-left corner so it never falls off-image.
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        lx, ly = left, top
        draw.rectangle([lx, ly, lx + tw + 6, ly + th + 6], fill=color)
        draw.text((lx + 3, ly + 2), label, fill=(255, 255, 255), font=font)

    return out


def crop_to(
    img: Image.Image,
    window_origin: Tuple[int, int],
    rect: Dict[str, int],
    padding: int = 0,
) -> Tuple[Image.Image, Dict[str, Any]]:
    """Crop a window capture to one region, given in screen pixels.

    PrintWindow can only render a whole window, so the saving is not in the
    capture - it is in what gets encoded and sent. Inspecting two 70-dip rows in
    a 1400x804 frame costs an image token budget out of all proportion to the
    question, and the answer is easier to see cropped.

    The region is clamped to the captured frame and the clamping is reported: a
    control that is half outside its window is itself a finding, and silently
    returning a smaller crop would hide it. `clamped_fraction` says how much of
    the requested region survived, because "part of this is outside the window"
    reads the same at 95% as at 4% and only one of those is a near-empty image.
    """
    ox, oy = window_origin
    left = rect["left"] - ox - padding
    top = rect["top"] - oy - padding
    right = rect["right"] - ox + padding
    bottom = rect["bottom"] - oy + padding

    clamped_left = max(0, left)
    clamped_top = max(0, top)
    clamped_right = min(img.width, right)
    clamped_bottom = min(img.height, bottom)

    if clamped_right <= clamped_left or clamped_bottom <= clamped_top:
        raise CaptureError(
            f"crop region {rect} lies entirely outside the window capture "
            f"({img.width}x{img.height}) - the control is off-screen"
        )

    requested_area = max(1, (right - left) * (bottom - top))
    delivered_area = (clamped_right - clamped_left) * (clamped_bottom - clamped_top)
    fraction = round(delivered_area / requested_area, 3)

    info: Dict[str, Any] = {
        "crop": {
            "left": clamped_left, "top": clamped_top,
            "width": clamped_right - clamped_left,
            "height": clamped_bottom - clamped_top,
        },
        "padding": padding,
        "clamped_fraction": fraction,
    }
    if (clamped_left, clamped_top, clamped_right, clamped_bottom) != (left, top, right, bottom):
        info["clamped"] = (
            f"clamped to the window: {round(fraction * 100)}% of the requested "
            "region lies inside the window bounds"
        )
    return img.crop((clamped_left, clamped_top, clamped_right, clamped_bottom)), info


def downscale(img: Image.Image, max_width: Optional[int] = None) -> Image.Image:
    """Resize to `max_width`, or return the image untouched.

    Split out of `to_png_bytes` so the caller can report the size that was
    actually delivered. The downscale happens *after* the crop, so the crop's
    own numbers describe an image nobody received - which is the third
    unlabelled frame in a screenshot response.
    """
    if max_width and img.width > max_width:
        ratio = max_width / img.width
        return img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
    return img


def to_png_bytes(img: Image.Image, max_width: Optional[int] = None) -> bytes:
    img = downscale(img, max_width)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def to_png_base64(img: Image.Image, max_width: Optional[int] = None) -> str:
    return base64.b64encode(to_png_bytes(img, max_width)).decode("ascii")
