"""Exercise the real batch control flow with publisher/LLM stages replaced by stubs."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe batch contract")
@pytest.mark.parametrize("observer_exit", [0, 2])
def test_intraday_runs_observer_and_records_its_failure(tmp_path, observer_exit):
    original = Path(__file__).resolve().parents[1] / "run_intraday.bat"
    batch = tmp_path / "run_intraday.bat"
    text = original.read_text(encoding="utf-8").replace(
        'set "PY=%~dp0.venv\\Scripts\\python.exe"', f'set "PY={sys.executable}"')
    batch.write_text(text, encoding="utf-8")
    (tmp_path / "run.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "with Path('calls.txt').open('a') as f: f.write(' '.join(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8")
    (tmp_path / "health").mkdir()
    (tmp_path / "health" / "analyze.py").write_text(
        "from pathlib import Path\n"
        "with Path('calls.txt').open('a') as f: f.write('analyze\\n')\n"
        f"raise SystemExit({observer_exit})\n", encoding="utf-8")
    result = subprocess.run(["cmd.exe", "/d", "/c", str(batch),
                             "--exclude", "zeit.de"], cwd=tmp_path,
                            capture_output=True, timeout=30)
    assert result.returncode == observer_exit, (result.stdout, result.stderr)
    marker = json.loads((tmp_path / "data" / "last_intraday_run.json").read_text())
    assert marker["stages"]["quality_health"] == observer_exit
    assert marker["worst_exit"] == observer_exit
    calls = (tmp_path / "calls.txt").read_text().splitlines()
    assert "collect --kind news --exclude zeit.de" in calls
    assert ("fetch-bodies --kind news --title-gate-client jt-express "
            "--exclude zeit.de") in calls
    alert = next(i for i, call in enumerate(calls) if call == "alert-gate")
    analyze = calls.index("analyze")
    record = next(i for i, call in enumerate(calls) if call.startswith("record-pass "))
    assert alert < analyze < record
