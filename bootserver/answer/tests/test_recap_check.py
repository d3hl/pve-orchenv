"""The e2e idempotence gate must fail when any host reports changes (proves the check can fail)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "recap_check", Path(__file__).resolve().parents[3] / "ansible" / "tests" / "recap_check.py")
recap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recap)

CLEAN = """PLAY RECAP *****
e2e-pve1                   : ok=40   changed=0    unreachable=0    failed=0    skipped=9    rescued=0    ignored=0
e2e-pve2                   : ok=40   changed=0    unreachable=0    failed=0    skipped=9    rescued=0    ignored=0
"""


def test_clean_run_passes():
    hosts, problems = recap.check(CLEAN)
    assert hosts == ["e2e-pve1", "e2e-pve2"] and problems == []


def test_changed_host_fails():
    _, problems = recap.check(CLEAN.replace("ok=40   changed=0", "ok=39   changed=1", 1))
    assert problems == ["e2e-pve1: changed=1 unreachable=0 failed=0"]


def test_unreachable_or_failed_fails():
    assert recap.check(CLEAN.replace("unreachable=0    failed=0", "unreachable=1    failed=0", 1))[1]
    assert recap.check(CLEAN.replace("failed=0", "failed=2", 1))[1]


def test_missing_recap_fails():
    assert recap.check("nothing here")[1] == ["no PLAY RECAP found in the output"]
