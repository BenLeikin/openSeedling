"""Tests: running helper programs."""

from harness import tools


def test_run_reports_how_a_helper_went():
    fine = tools.run(["sh", "-c", "printf hello"], "the job")
    assert fine.ok and fine.stdout == b"hello" and fine.error == ""

    failed = tools.run(["sh", "-c", "echo no such camera >&2; exit 3"], "the job")
    assert not failed.ok and failed.returncode == 3
    assert failed.error == "the job failed: no such camera"

    silent = tools.run(["sh", "-c", "exit 4"], "the job")
    assert silent.error == "the job failed: exit 4"

    slow = tools.run(["sleep", "5"], "the job", timeout=0.2)
    assert not slow.ok and slow.returncode is None and "timed out" in slow.error

    killed = tools.run(["sh", "-c", "kill -9 $$"], "the job")
    assert "out of memory" in killed.error, "SIGKILL is what the kernel's OOM killer sends"

    crashed = tools.run(["sh", "-c", "kill -11 $$"], "the job")
    assert crashed.error == "the job was killed by signal 11"


def test_a_helper_runs_first_in_line_for_the_oom_killer():
    score = tools.run(["cat", "/proc/self/oom_score_adj"], "the check")
    mine = open("/proc/self/oom_score_adj").read().strip()
    assert score.stdout.strip() == b"1000" and mine != "1000"
