#!/usr/bin/env python3
"""Write an invisible cursor theme for the touchscreen (scripts/kiosk.sh).

    python3 scripts/blank_cursor.py DIR

Creates DIR/default/cursors/<name> for every common cursor name, each a
1x1 fully transparent Xcursor image. The kiosk unit points XCURSOR_PATH at
DIR, so the compositor (cage, via wlroots) and the browser (cog, via
libwayland-cursor) both load "default" from here and draw nothing. A
touchscreen that also reports itself as a mouse otherwise leaves an arrow
parked on the screen.
"""

import struct
import sys
from pathlib import Path

# CSS cursor names plus the X11 names toolkits ask for; a name missing from
# the theme can make a client fall back to a visible cursor.
NAMES = """
default left_ptr arrow top_left_arrow pointer hand hand1 hand2 pointing_hand
text xterm ibeam vertical-text wait watch progress left_ptr_watch half-busy
crosshair cross tcross cell move fleur all-scroll grab grabbing openhand
closedhand dnd-move dnd-copy dnd-link dnd-none copy alias no-drop not-allowed
help question_arrow whats_this context-menu col-resize row-resize
sb_h_double_arrow sb_v_double_arrow e-resize w-resize n-resize s-resize
ne-resize nw-resize se-resize sw-resize ew-resize ns-resize nesw-resize
nwse-resize right_side left_side top_side bottom_side top_right_corner
top_left_corner bottom_right_corner bottom_left_corner size_hor size_ver
size_bdiag size_fdiag zoom-in zoom-out none
""".split()

XCURSOR_IMAGE = 0xFFFD0002
NOMINAL = 24


def blank_cursor():
    """A one-image Xcursor file: 1x1, ARGB 0 (fully transparent)."""
    header = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1)
    toc = struct.pack("<III", XCURSOR_IMAGE, NOMINAL, 16 + 12)
    image = struct.pack("<IIIIIIIII", 36, XCURSOR_IMAGE, NOMINAL, 1, 1, 1, 0, 0, 0)
    return header + toc + image + struct.pack("<I", 0)


def write_theme(root):
    cursors = Path(root) / "default" / "cursors"
    cursors.mkdir(parents=True, exist_ok=True)
    data = blank_cursor()
    for name in NAMES:
        (cursors / name).write_bytes(data)
    (Path(root) / "default" / "index.theme").write_text(
        "[Icon Theme]\nName=openSeedling blank\nComment=No visible cursor\n"
    )
    return cursors


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(write_theme(sys.argv[1]))
