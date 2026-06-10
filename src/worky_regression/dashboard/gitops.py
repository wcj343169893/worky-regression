"""標記（markup）修改的 git 提交 / 回滾輔助。

worker 處理標記時記下動到的檔案（files_changed）；使用者按「已解決」→ 把這些檔案
提交成一個獨立 commit（sha 回寫標記）；按「回滾」→ 已提交者 git revert、未提交者
還原工作區。所有操作都限定在本倉根目錄、以檔案清單為界，避免波及其他工作。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=60, check=check)


def dirty_files() -> set[str]:
    """目前工作區有變動的檔案（含未追蹤），git status --porcelain 的路徑集合。"""
    out = _git("status", "--porcelain").stdout
    files = set()
    for line in out.splitlines():
        if len(line) > 3:
            # rename 形如 "R  old -> new"，取新路徑
            path = line[3:].split(" -> ")[-1].strip().strip('"')
            files.add(path)
    return files


def commit_markup(markup_id: int, files: list[str], summary: str) -> str:
    """把指定檔案提交成一個 commit，回傳 sha。檔案已不存在變動（被後續標記覆蓋等）會拋錯。"""
    existing = [f for f in files if f]
    if not existing:
        raise ValueError("此標記沒有記錄到檔案變動，無可提交")
    _git("add", "--", *existing)
    # 只提交這些檔案（index 中其他暫存不受影響——commit 帶路徑限定）
    msg = f"fix(markup#{markup_id}): {summary[:80]}\n\nCo-Authored-By: markup-worker <noreply@worky.local>"
    _git("commit", "-m", msg, "--", *existing)
    return _git("rev-parse", "HEAD").stdout.strip()


def rollback_markup(*, commit_sha: str | None, files: list[str]) -> str:
    """撤銷一筆標記的修改。已提交 → git revert（不動其他 commit）；未提交 → 還原工作區檔案。

    回傳人類可讀的處理說明。revert 衝突時 abort 並拋錯（不留半套狀態）。
    """
    if commit_sha:
        try:
            _git("revert", "--no-edit", commit_sha)
        except subprocess.CalledProcessError as e:
            _git("revert", "--abort", check=False)
            raise RuntimeError(f"git revert 衝突（已中止）：{e.stderr[:300]}") from e
        new_sha = _git("rev-parse", "HEAD").stdout.strip()
        return f"已 git revert {commit_sha[:10]} → 新 commit {new_sha[:10]}"
    if not files:
        raise ValueError("此標記沒有 commit 也沒有檔案變動記錄，無可回滾")
    tracked, untracked = [], []
    for f in files:
        if _git("ls-files", "--error-unmatch", f, check=False).returncode == 0:
            tracked.append(f)
        else:
            untracked.append(f)
    if tracked:
        _git("checkout", "--", *tracked)
    for f in untracked:  # worker 新建且未提交的檔案 → 直接刪除
        p = REPO_ROOT / f
        if p.is_file():
            p.unlink()
    return (f"已還原工作區檔案 {len(tracked)} 個"
            + (f"，刪除未追蹤新檔 {len(untracked)} 個" if untracked else ""))
