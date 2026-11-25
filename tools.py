"""Running helper programs: imgtool, ffmpeg and the camera tools, each as the
kernel's first choice to kill when memory runs out."""

import subprocess
import sys
from pathlib import Path

IMGTOOL = Path(__file__).with_name("imgtool.py")


def oom_first(cmd):
    """Wrap cmd so it runs as the kernel's first choice to kill when memory runs out.

    The wrapper raises its own oom_score_adj to the maximum before exec (this
    needs no privilege), so a capture or image job that exhausts memory is
    killed in place of the controller (lights, pumps, dashboard)."""
    return [
        "sh",
        "-c",
        'echo 1000 > /proc/self/oom_score_adj 2>/dev/null; exec "$@"',
        "oom-first",
    ] + [str(c) for c in cmd]


def imgtool(args, timeout=120):
    """Run an imgtool.py job in its own process; returns the CompletedProcess.

    Every OpenCV job goes through here: OpenCV costs about 40 MB once imported
    and never returns it, and in a subprocess it is freed when the job ends."""
    return subprocess.run(
        oom_first([sys.executable, str(IMGTOOL)] + [str(a) for a in args]),
        capture_output=True,
        timeout=timeout,
    )


def _imgtool_err(r):
    if r.returncode < 0 or r.returncode == 137:
        return killed_msg(r, "the image job") or f"killed by signal {-r.returncode}"
    return (r.stderr or b"").decode(errors="replace").strip()[-200:] or f"exit {r.returncode}"


def killed_msg(r, what):
    """A message naming an out-of-memory kill, or None if r did not die of one."""
    if r.returncode in (-9, 137):
        return (
            f"{what} was killed by the kernel for running out of memory; "
            "a smaller camera mode needs less (see Settings, Camera, Camera modes)"
        )
    return None
