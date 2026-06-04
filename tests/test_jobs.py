"""「工作」系統 path 回歸：對 cases/job-*.yaml 參數化。

角色：employer（商家 user_type=1）+ labor（打工夥伴 user_type=2）。
前置：v31x 需有雇主測試資料，未建請跑 `python scripts/bootstrap_job_env.py`。
"""
from pathlib import Path

import pytest

from worky_regression.runner import PathRunner

CASES_DIR = Path(__file__).resolve().parents[1] / "cases"
JOB_FILES = sorted(CASES_DIR.glob("job-*.yaml"))


@pytest.mark.job
@pytest.mark.parametrize("path_file", JOB_FILES, ids=lambda p: p.stem)
def test_job_path(path_file: Path, db, job_actors):
    runner = PathRunner(db)
    runner.run(path_file, actors=job_actors)
