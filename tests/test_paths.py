"""跑 cases/path-*.yaml 的端對端回歸測試。

注意：實際 endpoint / push type_id 還需逐一確認，目前 smoke 通過後再開啟。
"""
from pathlib import Path

import pytest

from worky_regression.runner import PathRunner


CASES_DIR = Path(__file__).resolve().parents[1] / "cases"
PATH_FILES = sorted(CASES_DIR.glob("path-*.yaml"))


@pytest.mark.path
@pytest.mark.parametrize("path_file", PATH_FILES, ids=lambda p: p.stem)
def test_path(path_file: Path, db, publisher, receiver):
    runner = PathRunner(db)
    runner.run(path_file, publisher=publisher, receiver=receiver)
