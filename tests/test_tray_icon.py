"""Unit tests for tray icon generation."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

# Ensure the conductor-dashboard package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tray import make_icon, ICON_SIZE, _BLUE, _ORANGE, _GREEN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert '#rrggbb' to (r, g, b)."""
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _region_has_color(
    img: Image.Image,
    x0: int, y0: int, x1: int, y1: int,
    target_rgb: tuple[int, int, int],
    tolerance: int = 40,
    min_pixels: int = 5,
) -> bool:
    """Return True if at least *min_pixels* in the rectangle match *target_rgb*."""
    count = 0
    for x in range(x0, x1):
        for y in range(y0, y1):
            r, g, b, a = img.getpixel((x, y))
            if a < 128:
                continue
            if (
                abs(r - target_rgb[0]) <= tolerance
                and abs(g - target_rgb[1]) <= tolerance
                and abs(b - target_rgb[2]) <= tolerance
            ):
                count += 1
                if count >= min_pixels:
                    return True
    return False


def _region_is_transparent(
    img: Image.Image,
    x0: int, y0: int, x1: int, y1: int,
    alpha_threshold: int = 30,
) -> bool:
    """Return True if all pixels in the rectangle are (nearly) transparent."""
    for x in range(x0, x1):
        for y in range(y0, y1):
            _, _, _, a = img.getpixel((x, y))
            if a > alpha_threshold:
                return False
    return True


# ---------------------------------------------------------------------------
# 1. Base icon states
# ---------------------------------------------------------------------------

class TestBaseIcon:
    def test_hollow_icon_when_no_active(self) -> None:
        img = make_icon(active=0, gates_waiting=0)
        # Centre 10×10 area should be transparent (hollow)
        assert _region_is_transparent(img, 27, 27, 37, 37)

    def test_filled_green_icon_when_active(self) -> None:
        img = make_icon(active=1, gates_waiting=0)
        green = _hex_to_rgb(_GREEN)
        # Centre area should contain green fill
        assert _region_has_color(img, 20, 20, 44, 44, green)


# ---------------------------------------------------------------------------
# 2. Active count badge
# ---------------------------------------------------------------------------

class TestActiveBadge:
    def test_no_badge_when_zero_active(self) -> None:
        img = make_icon(active=0, gates_waiting=0)
        blue = _hex_to_rgb(_BLUE)
        # Top-left quadrant should NOT have blue
        assert not _region_has_color(img, 0, 0, 24, 24, blue)

    def test_badge_shows_count_1(self) -> None:
        img = make_icon(active=1, gates_waiting=0)
        blue = _hex_to_rgb(_BLUE)
        assert _region_has_color(img, 0, 0, 24, 24, blue)

    def test_badge_shows_count_9(self) -> None:
        img = make_icon(active=9, gates_waiting=0)
        blue = _hex_to_rgb(_BLUE)
        assert _region_has_color(img, 0, 0, 24, 24, blue)

    def test_badge_shows_star_for_10_plus(self) -> None:
        img = make_icon(active=10, gates_waiting=0)
        blue = _hex_to_rgb(_BLUE)
        assert _region_has_color(img, 0, 0, 24, 24, blue)

        # Also verify active=15 uses the same badge
        img2 = make_icon(active=15, gates_waiting=0)
        assert _region_has_color(img2, 0, 0, 24, 24, blue)


# ---------------------------------------------------------------------------
# 3. Gate waiting badge
# ---------------------------------------------------------------------------

class TestGateBadge:
    def test_no_gate_badge_when_no_gates(self) -> None:
        img = make_icon(active=0, gates_waiting=0)
        orange = _hex_to_rgb(_ORANGE)
        # Top-right quadrant should NOT have orange
        assert not _region_has_color(img, 40, 0, 64, 24, orange)

    def test_gate_badge_when_gates_waiting(self) -> None:
        img = make_icon(active=0, gates_waiting=1)
        orange = _hex_to_rgb(_ORANGE)
        assert _region_has_color(img, 40, 0, 64, 24, orange)


# ---------------------------------------------------------------------------
# 4. Combined states
# ---------------------------------------------------------------------------

class TestCombinedStates:
    def test_both_badges_combined(self) -> None:
        img = make_icon(active=3, gates_waiting=2)
        blue = _hex_to_rgb(_BLUE)
        orange = _hex_to_rgb(_ORANGE)
        assert _region_has_color(img, 0, 0, 24, 24, blue)
        assert _region_has_color(img, 40, 0, 64, 24, orange)

    def test_only_gate_badge_no_active(self) -> None:
        img = make_icon(active=0, gates_waiting=1)
        blue = _hex_to_rgb(_BLUE)
        orange = _hex_to_rgb(_ORANGE)
        # Gate badge present, active badge absent
        assert _region_has_color(img, 40, 0, 64, 24, orange)
        assert not _region_has_color(img, 0, 0, 24, 24, blue)


# ---------------------------------------------------------------------------
# 5. Icon generation sanity
# ---------------------------------------------------------------------------

class TestIconSanity:
    def test_icon_returns_valid_image(self) -> None:
        img = make_icon(active=1, gates_waiting=1)
        assert isinstance(img, Image.Image)

    def test_icon_correct_size(self) -> None:
        img = make_icon(active=0, gates_waiting=0)
        assert img.size == (ICON_SIZE, ICON_SIZE)
        assert img.mode == "RGBA"
