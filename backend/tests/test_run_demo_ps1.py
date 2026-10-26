"""Checks on the Windows start script `run_demo.ps1`.

Fast, offline, no key, no dataset. The integration-style tests invoke the
script with -CheckOnly (never a real demo run) and are skipped on non-Windows
because they need powershell.exe / pwsh.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "run_demo.ps1"
SH_SCRIPT = ROOT / "run_demo.sh"

IS_WINDOWS = sys.platform.startswith("win")


def _powershell() -> str | None:
    for name in ("powershell.exe", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    return None


# ---------- plain file checks (run anywhere) ----------


def test_run_demo_ps1_exists():
    assert SCRIPT.exists(), f"missing: {SCRIPT}"


def test_run_demo_ps1_is_pure_ascii():
    data = SCRIPT.read_bytes()
    bad = [(i, b) for i, b in enumerate(data) if b > 0x7F]
    assert not bad, f"non-ASCII bytes at offsets {bad[:5]} (PowerShell 5.1 reads BOM-less UTF-8 as ANSI)"


def test_run_demo_ps1_has_strict_mode_and_stop():
    text = SCRIPT.read_text(encoding="ascii")
    assert "Set-StrictMode -Version Latest" in text
    assert "$ErrorActionPreference = 'Stop'" in text


def test_run_demo_ps1_rejects_pwsh7_only_syntax():
    text = SCRIPT.read_text(encoding="ascii")
    # Pipeline chain operators are a 5.1 parse error.
    assert " && " not in text, "pipeline chain operator && is PS7-only"
    assert " || " not in text, "pipeline chain operator || is PS7-only"
    # Null-coalescing / null-conditional are PS7-only.
    assert " ?? " not in text, "null-coalescing is PS7-only"
    assert "?." not in text or "?.exe" in text, "null-conditional is PS7-only"
    # ForEach-Object -Parallel is PS7-only.
    assert "-Parallel" not in text, "ForEach-Object -Parallel is PS7-only"
    # The ternary operator uses a bare '?' between expressions.
    # Avoid naive matching; look for a condition-shaped use.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if " ? " in stripped and " : " in stripped and not stripped.startswith("<#"):
            # Allow string-literal uses, flag anything that looks like `(cond) ? a : b`.
            if ") ? " in stripped:
                pytest.fail(f"PS7-only ternary detected: {stripped}")


def test_run_demo_sh_untouched_vs_git():
    if shutil.which("git") is None:
        pytest.skip("git not available")
    result = subprocess.run(
        ["git", "diff", "--exit-code", "--", "run_demo.sh"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"run_demo.sh changed:\n{result.stdout}"


# ---------- PowerShell-driven checks (Windows only) ----------

ps_only = pytest.mark.skipif(
    not IS_WINDOWS or _powershell() is None,
    reason="windows-only: needs powershell.exe or pwsh",
)


@ps_only
def test_ps_parses_without_error():
    ps = _powershell()
    cmd = (
        "$errors = $null; $tokens = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors) { $errors | ForEach-Object { Write-Host $_.Message }; exit 1 } else { exit 0 }"
    )
    result = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"parse errors:\n{result.stdout}\n{result.stderr}"


@ps_only
def test_check_only_passes_on_this_machine_and_hides_key_value():
    ps = _powershell()
    fake_key = "AKX_FAKE_KEY_FINGERPRINT_1234567890"
    env = dict(os.environ)
    env["GROQ_API_KEY"] = fake_key
    result = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(SCRIPT), "-CheckOnly"],
        capture_output=True,
        text=True,
        env=env,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"-CheckOnly failed:\n{combined}"
    assert "Python" in combined and "Node" in combined, combined
    assert fake_key not in combined, "the fake key leaked into script output"


@ps_only
def test_check_only_reports_missing_venv_with_fix_hint(tmp_path):
    ps = _powershell()
    result = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(SCRIPT), "-CheckOnly",
         "-ProjectRoot", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Virtual environment missing" in combined, combined
    assert ".venv" in combined


@ps_only
def test_check_only_reports_busy_api_port():
    ps = _powershell()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    busy_port = sock.getsockname()[1]
    try:
        result = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(SCRIPT), "-CheckOnly", "-ApiPort", str(busy_port)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        sock.close()
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert str(busy_port) in combined and "busy" in combined.lower(), combined
