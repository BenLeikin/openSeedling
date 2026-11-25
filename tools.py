"""Running helper programs: imgtool, ffmpeg and the camera tools.

Every helper runs as the kernel's first choice to kill when memory runs out,
so an image job that exhausts memory dies in place of the controller (lights,
pumps, dashboard). run() is the one way to start one, and reports a timeout,
an out-of-memory kill or a failure the same way for all of them.
"""

import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

IMGTOOL = Path(__file__).with_name("imgtool.py")


@dataclass
class Result:
    """How a helper run went. `error` is empty on success; otherwise it says
    what went wrong, ready to log or show. `returncode` is None when the
    helper never finished (it timed out, or could not be started)."""

    returncode: int | None
    stdout: bytes
    stderr: bytes
    error: str

    @property
    def ok(self) -> bool:
        return not self.error


def oom_first(cmd: Sequence[object]) -> list[str]:
    """cmd wrapped to run with its oom_score_adj at the maximum. Raising your
    own score needs no privilege."""
    return [
        "sh",
        "-c",
        'echo 1000 > /proc/self/oom_score_adj 2>/dev/null; exec "$@"',
        "oom-first",
    ] + [str(c) for c in cmd]


def run(cmd: Sequence[object], what: str, timeout: float = 120) -> Result:
    """Run a helper to completion and return a Result. `what` names the job
    in the error text ("the thumbnail", "stitching"). Does not raise for
    anything the helper does."""
    try:
        r = subprocess.run(oom_first(cmd), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(None, b"", b"", f"{what} timed out after {timeout:g} s")
    except OSError as e:
        return Result(None, b"", b"", f"{what} could not start: {e}")
    return Result(
        r.returncode, r.stdout or b"", r.stderr or b"", _why(r.returncode, r.stderr, what)
    )


def _why(returncode: int, stderr: bytes | None, what: str) -> str:
    if returncode == 0:
        return ""
    if returncode in (-9, 137):
        return (
            f"{what} was killed by the kernel for running out of memory; "
            "a smaller camera mode needs less (see Settings, Camera, Camera modes)"
        )
    if returncode < 0:
        return f"{what} was killed by signal {-returncode}"
    said = (stderr or b"").decode(errors="replace").strip()[-200:]
    return f"{what} failed: {said or f'exit {returncode}'}"


def imgtool(args: Sequence[object], what: str = "the image job", timeout: float = 120) -> Result:
    """Run an imgtool.py job; returns a Result.

    Every OpenCV job goes through here: OpenCV costs about 40 MB once imported
    and never returns it, and in a process of its own it is freed when the
    job ends."""
    return run([sys.executable, str(IMGTOOL)] + [str(a) for a in args], what, timeout)
