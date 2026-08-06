from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_verify_script_suppresses_uv_lock_chatter(tmp_path):
    project_root = Path(__file__).parents[2]
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        """#!/bin/sh
if [ "$1" = "lock" ]; then
  printf '%s\n' 'lock chatter must stay hidden' >&2
  exit 0
fi
if [ "$1" = "run" ]; then
  printf '%s\n' '{"status":"verified"}'
  exit 0
fi
exit 9
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    completed = subprocess.run(
        [str(project_root / "scripts" / "verify_v01.sh")],
        cwd=project_root,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == '{"status":"verified"}\n'
    assert completed.stderr == ""
