"""被測倉錯誤碼目錄（ApiExceptionCode.php）解析 + 領域分組。

AI 用例分解「子用例」要結合完整錯誤碼來補覆蓋缺口（見 README「AI 分解」）：
- 解析 `/www/wwwroot/worky/api/base/Exception/ApiExceptionCode.php` 成 {code -> 名稱/註解}。
- 提供 `group_for(code, actor)` 把一條負向子用例歸到「商家端 / 打工夥伴 / 承攬制 / 共通 / 系統」，
  與看板樹狀第一層對齊。分組以**步驟 actor 為主**（商家端=employer、打工夥伴=labor、
  承攬制=publisher/receiver），跨切面碼（4xxxx 共通、1xxxx/8xxx/9xxx 系統）則不論步驟歸到共通/系統。

來源檔以 `WORKY_SRC_DIR`（config.py，預設 /www/wwwroot/worky）為準；解析結果依 mtime 快取。
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import WORKY_SRC_DIR

# ── 領域分組（樹狀第一層）──────────────────────────────────────────────────────
# key 穩定排序：商家端 → 打工夥伴 → 承攬制 → 共通 → 系統。label 給前端標題直接用。
GROUP_ORDER = ["employer", "labor", "contract", "common", "system"]
GROUP_LABELS = {
    "employer": "商家端用例",
    "labor": "打工夥伴用例",
    "contract": "承攬制用例",
    "common": "共通用例",
    "system": "系統用例",
}
# 步驟 actor → 角色領域（actor 為主的分組依據）
_ACTOR_GROUP = {
    "employer": "employer",
    "labor": "labor",
    "publisher": "contract",
    "receiver": "contract",
}

_CONST_RE = re.compile(r"public\s+const\s+([A-Z0-9_]+)\s*=\s*(\d+)\s*;(?:\s*//\s*(.*))?")
_INLINE_COMMENT_RE = re.compile(r"^\s*//\s?(.*)$")

_cache: dict[str, object] = {"mtime": None, "by_code": {}, "path": None}


def _catalog_path(worky_dir: str = WORKY_SRC_DIR) -> Path:
    return Path(worky_dir) / "api" / "base" / "Exception" / "ApiExceptionCode.php"


def load_catalog(worky_dir: str = WORKY_SRC_DIR) -> dict[int, dict]:
    """解析 ApiExceptionCode.php → {code:int -> {code, name, comment}}（依 mtime 快取）。

    label 取用順序：常數前一段 // 或 /** */ 註解 ＞ 行尾 // 註解 ＞ 常數名 humanize。
    檔案不存在時回空 dict（呼叫端據此優雅降級成「只用已建模 branches」）。
    """
    path = _catalog_path(worky_dir)
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    if _cache["mtime"] == mtime and _cache["path"] == str(path):
        return _cache["by_code"]  # type: ignore[return-value]

    by_code: dict[int, dict] = {}
    pending: list[str] = []  # 累積到下一個 const 為止的前置註解行
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        m = _CONST_RE.search(line)
        if m:
            name, code_s, trailing = m.group(1), m.group(2), (m.group(3) or "").strip()
            comment = trailing or " ".join(pending).strip()
            by_code[int(code_s)] = {
                "code": int(code_s),
                "name": name,
                "comment": comment,
            }
            pending = []
            continue
        # /** 多行 doc */ 與單行 // 註解都收進 pending，遇到 const 才結算；空行/其他清空
        cm = _INLINE_COMMENT_RE.match(line)
        if cm:
            pending.append(cm.group(1).strip())
        elif line.startswith("*"):
            pending.append(line.lstrip("*/ ").strip())
        elif line.startswith("/**") or line.startswith("/*"):
            pending = []  # 開始一段 doc block
            body = line.lstrip("/* ").rstrip("*/ ").strip()
            if body:
                pending.append(body)
        elif not line or line.startswith(("final", "namespace", "<?php", "}")):
            if not line:
                pending = []
    _cache.update({"mtime": mtime, "by_code": by_code, "path": str(path)})
    return by_code


def describe(code: int, worky_dir: str = WORKY_SRC_DIR) -> dict:
    """單一錯誤碼的可讀資訊：{code, name, comment, label}。查無時回 name 留空的 stub。"""
    info = load_catalog(worky_dir).get(int(code))
    if not info:
        return {"code": int(code), "name": "", "comment": "", "label": str(code)}
    label = info["comment"] or _humanize(info["name"])
    return {**info, "label": label}


def _humanize(name: str) -> str:
    """常數名退化成可讀字串（沒有中文註解時的後備）：SHOP_NOT_FOUND → Shop Not Found。"""
    return name.replace("_", " ").title()


def group_for(code: int | None, actor: str | None) -> tuple[str, str]:
    """把一條負向子用例歸到樹狀第一層，回 (group_key, group_label)。

    規則（對齊看板預期）：
      - 跨切面碼優先：4xxxx → 共通；1xxxx / 8xxx / 9xxx → 系統（不論步驟 actor）。
      - 其餘以步驟 actor 為主：employer→商家端、labor→打工夥伴、publisher/receiver→承攬制。
        例：J1(employer) 步的 30205(SHOP_NOT_FOUND) 仍歸商家端，而非依 3xxxx 落到打工夥伴。
      - actor 缺失 / 不認得時，退回依碼段（2xxxx 商家、3xxxx 打工、5xxxx 承攬、其餘共通）。
    """
    c = int(code) if code is not None else None
    if c is not None:
        if 40000 <= c < 50000:
            return "common", GROUP_LABELS["common"]
        if 10000 <= c < 20000 or 8000 <= c < 10000:
            return "system", GROUP_LABELS["system"]
    g = _ACTOR_GROUP.get((actor or "").strip())
    if g:
        return g, GROUP_LABELS[g]
    # actor 不可用 → 退回碼段
    if c is not None:
        if 20000 <= c < 30000:
            return "employer", GROUP_LABELS["employer"]
        if 30000 <= c < 40000:
            return "labor", GROUP_LABELS["labor"]
        if 50000 <= c < 60000:
            return "contract", GROUP_LABELS["contract"]
    return "common", GROUP_LABELS["common"]


def catalog_for_actors(actors: set[str], worky_dir: str = WORKY_SRC_DIR) -> list[dict]:
    """給定流程涉及的 actor，回「與這些角色相關 + 共通」的錯誤碼子集（餵 LLM 用，壓 token）。

    依 group_for 反推：保留會落到這些 actor 所屬領域、或共通/系統的碼。回 describe() 形狀清單。
    """
    keep = {_ACTOR_GROUP.get(a) for a in actors} | {"common", "system"}
    keep.discard(None)
    out: list[dict] = []
    for code in sorted(load_catalog(worky_dir)):
        gk, _ = group_for(code, None)  # 無 actor → 純依碼段歸領域
        if gk in keep:
            out.append(describe(code, worky_dir))
    return out
