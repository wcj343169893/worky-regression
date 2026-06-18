"""真機軌（B 軌）執行器：把一條 Maestro 用例逐步跑在真機上，結果落地 worky_qa_dashboard。

與 API 軌（`recorder.RecordingRunner`）走不同層——驅動的是 App **UI**（點擊/輸入/斷言畫面）
而非後端狀態機（HTTP + 簽名）——但**共用同一套持久化契約**（qa_runs / qa_run_steps）
與 **SSE 事件協定**（on_event 的 run_start / step_start / step_end / run_end），所以看板的
「執行 / 歷史 / 最近結果」完全沿用，server.py 與前端零改。

每一步 = 一段 Maestro flow（一或多個指令）：DeviceRunner 把它寫成一個帶 ``appId`` 表頭的
暫存 flow 檔，以 ``maestro test`` subprocess 跑在指定真機上。exit code 0 = passed，
stdout/stderr 尾段存進 observations 供排查。App 狀態在步驟間自然延續（不下 clearState
就不會重置），所以「步驟1 啟動 → 步驟2 斷言畫面」可行。

為什麼用 CLI subprocess 而非 Maestro MCP：背景 worker 與看板請求 thread **取不到 MCP 工具**，
maestro CLI binary（預設 ~/.maestro/bin/maestro，可用 WORKY_MAESTRO_BIN 覆寫）才是能被框架
驅動的執行通道。MCP 留給互動式編寫 / 除錯 flow（inspect_screen / 試跑單條 yaml）。

用例 YAML 形狀（system 由 dashboard.cases._detect_system 偵測 kind=maestro → "app"）：

    id: device-labor-home-smoke
    kind: maestro
    description: 真機冒煙：打工端 App 啟動並進入首頁
    device:
      app_id: dev.tw.com.worky.labor.and   # 必填：要驅動的 App 套件名
      device_id: ""                          # 選填：adb 序號；空 → 用 settings / 自動挑
    path:
      - maestro: { name: 啟動 App, flow: "- launchApp" }
      - maestro: { name: 首頁可見, flow: "- assertVisible: 找工作" }
"""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import yaml

from .config import Settings
from .qa_store import QAStore, make_run_id
# 複用 API 軌的結果型別，落庫形狀（to_dict）與看板讀取完全一致
from .recorder import RunResult, StepResult


class DeviceRunner:
    """跑一條 Maestro 用例並逐步記錄結果（不在失敗時 raise）；落地 worky_qa_dashboard。"""

    # 單步 maestro flow 預設逾時（秒）；用例可在 step.maestro.timeout 覆寫。
    DEFAULT_STEP_TIMEOUT = 120

    # Maestro 的 Android driver 兩支 APK（內嵌在 maestro-client.jar）。
    DRIVER_PKG = "dev.mobile.maestro"          # maestro-app.apk
    SERVER_PKG = "dev.mobile.maestro.test"     # maestro-server.apk（instrumentation）
    # MIUI/小米「USB 安裝」確認框的 Activity 與按鈕文字（簡繁都收）。
    _MIUI_INSTALL_ACTIVITY = "AdbInstallActivity"
    _CONFIRM_TEXTS = ("继续安装", "繼續安裝")

    def __init__(self, settings: Settings, *, qa_store: QAStore | None = None,
                 system: str = "app", lock_wait_sec: float = 0.0):
        """lock_wait_sec：取不到裝置鎖時最多等幾秒。

        單裝置必須序列化：看板 inline 執行（請求 thread）與 device_worker（背景）共用同一台機，
        兩個 maestro/截圖 session 同時打會互相干擾。DeviceRunner 在實際碰裝置前取一把
        **跨進程**裝置鎖（flock on /tmp/worky-device-<id>.lock）。看板預設 0 = 取不到就快速失敗
        （回「裝置忙碌中」，UX 好過卡住）；device_worker 傳大值 = 排隊等到輪到它。
        """
        self.settings = settings
        self.qa_store = qa_store
        self.system = system
        self.lock_wait_sec = lock_wait_sec

    # ── 跨進程裝置鎖（序列化單裝置存取）─────────────────────────────────────────
    @contextmanager
    def _device_lock(self, device_id: str):
        path = Path(tempfile.gettempdir()) / f"worky-device-{device_id}.lock"
        fh = open(path, "w")  # noqa: SIM115 — 須活到釋放鎖
        deadline = time.time() + max(0.0, self.lock_wait_sec)
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    fh.close()
                    raise AssertionError(
                        f"裝置 {device_id} 忙碌中：另一個真機執行正在進行（單裝置已序列化）。"
                        "請稍後重試，或改用 device_worker 排隊背景執行。")
                time.sleep(1)
        try:
            yield
        finally:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()

    # ── 裝置解析 ───────────────────────────────────────────────────────────────
    def _resolve_device_id(self, spec_device: dict) -> str:
        """決定要驅動哪台裝置：用例 device.device_id > settings > adb 唯一在線裝置。

        單機實驗室常態是「只接一台」，故未配置時自動挑 adb devices 的唯一在線裝置，
        讓用例不必寫死序號（序號是環境相關，不該進 committed 用例）。
        """
        explicit = str(spec_device.get("device_id") or "").strip() or self.settings.maestro_device_id
        if explicit:
            return explicit
        devices = self._adb_devices()
        if len(devices) == 1:
            return devices[0]
        raise RuntimeError(
            f"未指定真機 device_id 且 adb 在線裝置數={len(devices)}（{devices}）。"
            f"請設 WORKY_MAESTRO_DEVICE_ID 或在用例 device.device_id 指定。")

    @staticmethod
    def _adb_devices() -> list[str]:
        """回 adb 在線裝置序號清單（state=device）；adb 不在或無裝置回空。"""
        try:
            out = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10)
        except Exception:  # noqa: BLE001 — 無 adb / 逾時 → 視為無裝置
            return []
        ids: list[str] = []
        for line in out.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                ids.append(parts[0])
        return ids

    # ── driver 常駐：確保 maestro 兩支 driver APK 已裝，之後用 --no-reinstall-driver 複用 ──
    # 為什麼：maestro 預設每跑（甚至每步）都 uninstall+reinstall driver（AndroidDriver.reinstallDriver
    # =true）；MIUI「USB 安裝」沒開時每次都彈「繼續安裝」框、且重裝很慢。改成「首次裝好 → 之後
    # --no-reinstall-driver 跳過安裝」：包常駐、不再彈框、每步省一次卸裝重裝。首次安裝那一次的
    # MIUI 確認框由 _adb_install_with_confirm 自動點掉（可用 WORKY_MAESTRO_AUTO_CONFIRM=0 關閉）。
    def _installed_packages(self, device_id: str) -> set[str]:
        try:
            out = subprocess.run(["adb", "-s", device_id, "shell", "pm", "list", "packages"],
                                 capture_output=True, text=True, timeout=20)
        except Exception:  # noqa: BLE001
            return set()
        return {ln.split(":", 1)[1].strip() for ln in out.stdout.splitlines()
                if ln.startswith("package:")}

    def _driver_jar(self) -> Path | None:
        """從 maestro_bin 推出 lib/maestro-client.jar（內嵌兩支 driver APK）。"""
        lib = Path(self.settings.maestro_bin).resolve().parent.parent / "lib"
        jar = lib / "maestro-client.jar"
        return jar if jar.is_file() else None

    def _extract_driver_apks(self) -> dict[str, Path]:
        """把 maestro-app.apk / maestro-server.apk 從 jar 掏到暫存目錄，回 {pkg: apk_path}。"""
        jar = self._driver_jar()
        if jar is None:
            raise AssertionError(
                f"找不到 maestro-client.jar（由 {self.settings.maestro_bin} 推算）；"
                "無法取得 driver APK，請確認 maestro 安裝完整。")
        out_dir = Path(tempfile.mkdtemp(prefix="worky-maestro-driver-"))
        mapping = {self.DRIVER_PKG: "maestro-app.apk", self.SERVER_PKG: "maestro-server.apk"}
        result: dict[str, Path] = {}
        with zipfile.ZipFile(jar) as zf:
            for pkg, name in mapping.items():
                dst = out_dir / name
                with zf.open(name) as src, open(dst, "wb") as fh:
                    fh.write(src.read())
                result[pkg] = dst
        return result

    def _find_confirm_button(self, device_id: str) -> tuple[int, int] | None:
        """dump 當前 UI，找 MIUI「繼續安裝」可點按鈕中心座標；找不到回 None。"""
        try:
            subprocess.run(["adb", "-s", device_id, "shell", "uiautomator", "dump", "/sdcard/ui.xml"],
                           capture_output=True, text=True, timeout=15)
            pulled = Path(tempfile.gettempdir()) / f"worky-ui-{device_id}.xml"
            subprocess.run(["adb", "-s", device_id, "pull", "/sdcard/ui.xml", str(pulled)],
                           capture_output=True, text=True, timeout=15)
            root = ET.parse(pulled).getroot()
        except Exception:  # noqa: BLE001
            return None
        for n in root.iter("node"):
            if n.get("text", "") in self._CONFIRM_TEXTS and n.get("clickable") == "true":
                m = re.findall(r"\d+", n.get("bounds", ""))
                if len(m) == 4:
                    return (int(m[0]) + int(m[2])) // 2, (int(m[1]) + int(m[3])) // 2
        return None

    def _adb_install_with_confirm(self, device_id: str, apk: Path, timeout: int = 180) -> None:
        """adb install -r -t；安裝過程若 MIUI 彈「USB 安裝」框就自動點「繼續安裝」（除非關閉）。"""
        auto = os.environ.get("WORKY_MAESTRO_AUTO_CONFIRM", "1") != "0"
        proc = subprocess.Popen(["adb", "-s", device_id, "install", "-r", "-t", str(apk)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + timeout
        while proc.poll() is None and time.time() < deadline:
            if auto:
                try:
                    win = subprocess.run(["adb", "-s", device_id, "shell", "dumpsys", "window"],
                                         capture_output=True, text=True, timeout=10).stdout
                except Exception:  # noqa: BLE001
                    win = ""
                if self._MIUI_INSTALL_ACTIVITY in win:
                    coord = self._find_confirm_button(device_id)
                    if coord:
                        subprocess.run(["adb", "-s", device_id, "shell", "input", "tap",
                                        str(coord[0]), str(coord[1])],
                                       capture_output=True, text=True, timeout=10)
                        time.sleep(1.2)
            time.sleep(0.3)
        out = (proc.communicate()[0] or "") if proc.poll() is not None else ""
        if proc.poll() is None:
            proc.kill()
            raise AssertionError(f"adb install 逾時（{timeout}s）：{apk.name}")
        if "Success" not in out:
            hint = ""
            if "INSTALL_FAILED_USER_RESTRICTED" in out or self._MIUI_INSTALL_ACTIVITY:
                hint = ("（MIUI「USB 安裝」未開且自動點選未生效：請在手機開發者選項開啟"
                        "「通過 USB 安裝」後重試，或設 WORKY_MAESTRO_AUTO_CONFIRM=1 讓框架自動確認）")
            raise AssertionError(f"安裝 driver APK 失敗：{apk.name}\n{out[-500:]}{hint}")

    def _ensure_driver_installed(self, device_id: str) -> None:
        """兩支 driver APK 缺哪支補哪支（首次或被清掉後）；都在則秒回，不碰安裝。"""
        present = self._installed_packages(device_id)
        missing = [p for p in (self.DRIVER_PKG, self.SERVER_PKG) if p not in present]
        if not missing:
            return
        apks = self._extract_driver_apks()
        try:
            for pkg in missing:
                self._adb_install_with_confirm(device_id, apks[pkg])
        finally:
            for p in apks.values():
                try:
                    p.unlink()
                    p.parent.rmdir()
                except OSError:
                    pass

    # ── launch 步：用 adb 啟動 App（繞過 maestro launchApp 跳桌面）──────────────
    # 實測：maestro 的 launchApp 對本 App 會跳回桌面（疑似反 instrumentation 偵測），
    # 但 adb 的 launcher intent 能穩定把 App 拉到前景，且之後 maestro 用 instrumentation
    # 做截圖/斷言時 App 不會被踢走。故啟動一律走 adb，maestro flow 只做「App 已在前景」的操作。
    def _run_launch(self, app_id: str, device_id: str, opts: dict) -> dict[str, Any]:
        if not app_id:
            raise AssertionError("launch 步需要 device.app_id 才能啟動 App")
        force_stop = bool(opts.get("force_stop", True))   # 預設冷啟動（force-stop 再起）
        wait = float(opts.get("wait", 5))                 # 啟動後等畫面穩定（秒）
        if force_stop:
            subprocess.run(["adb", "-s", device_id, "shell", "am", "force-stop", app_id],
                           capture_output=True, text=True, timeout=20)
        proc = subprocess.run(
            ["adb", "-s", device_id, "shell", "monkey", "-p", app_id,
             "-c", "android.intent.category.LAUNCHER", "1"],
            capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            raise AssertionError(f"adb 啟動 App 失敗：{(proc.stderr or proc.stdout)[-500:]}")
        time.sleep(wait)
        return {"launched": app_id, "wait": wait, "force_stop": force_stop}

    # ── 單步執行：一段 flow → maestro test subprocess ─────────────────────────
    def _run_flow(self, app_id: str, device_id: str, flow: str,
                  timeout: int) -> dict[str, Any]:
        """把一段 flow 寫成帶 appId 表頭的暫存檔，以 maestro test 跑在真機上。

        回 {exit_code, output_tail, flow}；exit_code=0 即該步通過。非 0（含逾時）拋
        AssertionError，由上層記成 failed（與 API 軌的失敗落庫路徑一致）。
        """
        doc = f"appId: {app_id}\n---\n{flow.strip()}\n"
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                         encoding="utf-8") as f:
            f.write(doc)
            tmp = f.name
        try:
            try:
                # --no-reinstall-driver：driver 已由 _ensure_driver_installed 常駐，maestro
                # 不再每步卸裝重裝（省時、且不再觸發 MIUI「USB 安裝」確認框）。
                proc = subprocess.run(
                    [self.settings.maestro_bin, "--device", device_id, "test",
                     "--no-reinstall-driver", tmp],
                    capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise AssertionError(f"maestro flow 逾時（{timeout}s）")
            tail = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
            if proc.returncode != 0:
                # 把常見的裝置側阻斷翻成可行動的提示（框架無法用 adb 繞過，性質同 php-fpm restart）。
                if "INSTALL_FAILED_USER_RESTRICTED" in tail or "dev.mobile.maestro" in tail:
                    raise AssertionError(
                        "maestro driver 未就緒：裝置擋下了 USB 安裝（INSTALL_FAILED_USER_RESTRICTED）"
                        "或 driver 不在。框架已在開跑前以 _ensure_driver_installed 自動裝過一次並"
                        "自動點「繼續安裝」；若仍失敗，請在 MIUI 開發者選項開啟「通過 USB 安裝」後重試，"
                        "或設 WORKY_MAESTRO_AUTO_CONFIRM=1。")
                raise AssertionError(
                    f"maestro test 失敗（exit={proc.returncode}）：\n{tail}")
            return {"exit_code": proc.returncode, "output_tail": tail, "flow": flow.strip()}
        finally:
            try:
                Path(tmp).unlink()
            except OSError:
                pass

    # ── assert_ai 步：截圖 → 視覺大模型對畫面做自然語言斷言 ─────────────────────
    # 被測 App 是 Compose、文字不暴露無障礙樹（native uiautomator dump 只見廣告彈窗一個
    # 文字節點），故 maestro 的 text assertVisible 不可行。改用 adb screencap 抓整屏，
    # 餵 qwen-vl-max（DashScope OpenAI 相容）判定「畫面是否符合預期」。截圖走 adb exec-out
    # 而非 maestro，免 instrumentation、快且穩。
    @staticmethod
    def _screencap_png(device_id: str) -> bytes:
        """adb exec-out screencap -p 取整屏 PNG bytes（exec-out 不會把 \\n 轉 \\r\\n 弄壞二進位）。"""
        proc = subprocess.run(["adb", "-s", device_id, "exec-out", "screencap", "-p"],
                              capture_output=True, timeout=30)
        if proc.returncode != 0 or not proc.stdout:
            raise AssertionError(f"adb 截圖失敗：{(proc.stderr or b'')[:300]!r}")
        return proc.stdout

    def _run_assert_ai(self, device_id: str, step: dict) -> dict[str, Any]:
        """截圖 → qwen-vl-max 判定 step.prompt 描述的畫面條件是否成立。

        模型被要求只回 JSON {"pass": bool, "reason": "..."}；pass=false 拋 AssertionError
        （記為 failed），其餘記為通過。observations 保留 prompt / 判定 / 理由 / 模型供排查。
        """
        prompt = str(step.get("prompt") or "").strip()
        if not prompt:
            raise AssertionError("assert_ai 步缺少 prompt（要斷言的畫面條件）")
        if not self.settings.vision_api_key:
            raise AssertionError(
                "未設定視覺模型 key：請在 .env 設 WORKY_VISION_API_KEY（或 DASHSCOPE_API_KEY），"
                f"預設模型 {self.settings.vision_model}（阿里雲 DashScope）。")
        import base64
        png = self._screencap_png(device_id)
        b64 = base64.b64encode(png).decode("ascii")
        try:
            from openai import OpenAI
        except ImportError as e:
            raise AssertionError("未安裝 openai SDK，請 `pip install -e .[ai]`") from e
        client = OpenAI(api_key=self.settings.vision_api_key,
                        base_url=self.settings.vision_base_url)
        resp = client.chat.completions.create(
            model=self.settings.vision_model,
            messages=[
                {"role": "system", "content":
                 "你是行動 App UI 測試的視覺斷言器。依使用者描述判斷截圖是否符合，"
                 "只回 JSON：{\"pass\": true/false, \"reason\": \"簡短理由\"}，不要其它文字。"},
                {"role": "user", "content": [
                    {"type": "text", "text": f"斷言條件：{prompt}"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]},
            ],
            temperature=0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        verdict = self._parse_verdict(raw)
        obs = {"prompt": prompt, "model": self.settings.vision_model,
               "pass": verdict["pass"], "reason": verdict.get("reason", ""), "raw": raw[:500]}
        if not verdict["pass"]:
            raise AssertionError(f"視覺斷言不成立：{verdict.get('reason') or raw[:300]}")
        return obs

    @staticmethod
    def _parse_verdict(raw: str) -> dict[str, Any]:
        """從模型回應抽出 {pass, reason}；容忍 ```json 圍欄與前後雜訊。"""
        import json
        import re
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                d = json.loads(m.group(0))
                return {"pass": bool(d.get("pass")), "reason": str(d.get("reason", ""))}
            except (ValueError, TypeError):
                pass
        # 退化：回應裡找肯定/否定詞（模型沒守 JSON 時的兜底）
        low = raw.lower()
        ok = ("true" in low or "通過" in raw or "符合" in raw) and not (
            "false" in low or "不符" in raw or "不通過" in raw)
        return {"pass": ok, "reason": raw[:200]}

    # ── 主流程：逐步執行 + 逐步落庫 + SSE 事件 ─────────────────────────────────
    def run(self, spec: dict, *, on_event: Callable[[str, dict], None] | None = None,
            write: bool = True, started_at: int | None = None) -> RunResult:
        path_id = spec.get("id")
        if not path_id:
            raise ValueError("真機用例缺少 id：每筆用例都必須有唯一 id 才能落庫追溯")
        device = spec.get("device") or {}
        app_id = str(device.get("app_id") or "").strip()
        steps_spec = spec.get("path") or []
        ts = started_at if started_at is not None else int(time.time())
        run_id = make_run_id(path_id, ts)
        desc = str(spec.get("description", "")).strip()

        def emit(etype: str, payload: dict) -> None:
            if on_event is None:
                return
            try:
                on_event(etype, payload)
            except Exception:  # noqa: BLE001 — 推送失敗不可中斷執行
                pass

        skipped = bool(spec.get("skip"))
        skip_reason = str(spec.get("skip_reason", "")).strip()
        emit("run_start", {"run_id": run_id, "started_at": ts, "total": len(steps_spec),
                           "skipped": skipped, "skip_reason": skip_reason,
                           "transitions": []})   # 真機無 transition chip

        live = write and self.qa_store is not None
        if live:
            try:
                self.qa_store.begin_run(run_id=run_id, case_id=path_id, system=self.system,
                                        description=desc, started_at=ts, total=len(steps_spec))
            except Exception as e:  # noqa: BLE001
                live = False
                print(f"[device] 逐步落庫失敗，降級為跑完一次性落地：{e}")

        # 非 skip 才解析裝置（skip 用例不碰真機）；解析失敗整支記為 failed 而非崩潰。
        device_id = ""
        device_err: str | None = None
        if not skipped:
            try:
                device_id = self._resolve_device_id(device)
            except Exception as e:  # noqa: BLE001
                device_err = f"{type(e).__name__}: {e}"
            if not app_id and device_err is None:
                device_err = "用例 device.app_id 未指定，無法啟動 App"

        steps: list[StepResult] = []
        stopped = skipped or device_err is not None

        # 真要碰裝置才取跨進程裝置鎖（序列化單裝置）；取不到（忙碌超過 lock_wait）→
        # 記為 device_err 走失敗落庫，不崩潰、也不擾動正在跑的另一次執行。
        lock_ctx = None
        if not stopped:
            try:
                lock_ctx = self._device_lock(device_id)
                lock_ctx.__enter__()
                # driver 常駐：缺則補裝一次（首次自動點 MIUI 確認框）；之後各步 --no-reinstall-driver 複用。
                self._ensure_driver_installed(device_id)
            except AssertionError as e:
                device_err = f"{type(e).__name__}: {e}"
                stopped = True

        for i, st in enumerate(steps_spec):
            kind, name, run_fn = self._dispatch(st, i, app_id, device_id)
            # 真機步驟不對應 transition chip（tindex=None），前端 SSE 會當「非 chip 步驟」處理
            emit("step_start", {"index": i, "kind": kind, "name": name,
                                "tindex": None, "wait_secs": None, "next_tindex": None})
            if stopped:
                err = device_err if (device_err and not skipped) else None
                status = "failed" if err else "skipped"
                sr = StepResult(i, kind, name, status, 0, error=err)
                steps.append(sr)
                self._live_step(live, run_id, sr)
                emit("step_end", {"index": i, "status": status, "elapsed_ms": 0,
                                  "error": err, "tindex": None})
                device_err = None   # 裝置解析失敗只在第一步記原因，其餘記 skipped
                continue
            t0 = time.time()
            try:
                obs = run_fn()
                elapsed = int((time.time() - t0) * 1000)
                sr = StepResult(i, kind, name, "passed", elapsed, observations=obs or {})
                steps.append(sr)
                self._live_step(live, run_id, sr)
                emit("step_end", {"index": i, "status": "passed", "elapsed_ms": elapsed,
                                  "error": None, "tindex": None})
            except Exception as e:  # noqa: BLE001 — 記錄任何失敗
                elapsed = int((time.time() - t0) * 1000)
                err = f"{type(e).__name__}: {e}"
                sr = StepResult(i, kind, name, "failed", elapsed, error=err)
                steps.append(sr)
                self._live_step(live, run_id, sr)
                stopped = True   # 失敗即停（後續步驟記 skipped）
                emit("step_end", {"index": i, "status": "failed", "elapsed_ms": elapsed,
                                  "error": err, "tindex": None})

        if lock_ctx is not None:
            lock_ctx.__exit__(None, None, None)   # 釋放裝置鎖（flock 亦會在行程結束時自動釋放）

        failed_at = next((s.index for s in steps if s.status == "failed"), None)
        status = "failed" if failed_at is not None else "passed"
        if skipped:
            status = "skipped"
        result = RunResult(path_id=path_id, description=desc, started_at=ts, status=status,
                           steps=steps, failed_at=failed_at, run_id=run_id,
                           actors={"device": {"app_id": app_id, "device_id": device_id}})
        if write and self.qa_store is not None:
            self.qa_store.insert_run(
                run_id=run_id, case_id=path_id, system=self.system, status=status,
                description=desc, started_at=ts, failed_at=failed_at,
                steps=[asdict(s) for s in steps], source="run", actors=result.actors)
        emit("run_end", {"status": status, "failed_at": failed_at,
                        "skipped": skipped, "skip_reason": skip_reason,
                        "passed": sum(1 for s in steps if s.status == "passed"),
                        "total": len(steps)})
        return result

    def _dispatch(self, st: dict, i: int, app_id: str, device_id: str):
        """把一個 path 步驟對映成 (kind, name, 無參執行函式)。

        支援三種步驟：
          - launch:    {launch: {wait?, force_stop?}}            adb 啟動 App（繞 launchApp 跳桌面）
          - assert_ai: {assert_ai: {name?, prompt}}              截圖 → qwen-vl-max 視覺斷言
          - maestro:   {maestro: {name?, flow, timeout?}}        跑一段 Maestro flow
        """
        if "launch" in st:
            opts = st.get("launch") or {}
            name = str(opts.get("name") or f"啟動 {app_id}")
            return "launch", name, lambda: self._run_launch(app_id, device_id, opts)
        if "assert_ai" in st:
            spec = st.get("assert_ai") or {}
            name = str(spec.get("name") or "視覺斷言")
            return "assert_ai", name, lambda: self._run_assert_ai(device_id, spec)
        m = st.get("maestro") or {}
        name = str(m.get("name") or f"step{i}")
        flow = str(m.get("flow") or "")
        timeout = int(m.get("timeout", self.DEFAULT_STEP_TIMEOUT))
        return "maestro", name, lambda: self._run_flow(app_id, device_id, flow, timeout)

    def _live_step(self, live: bool, run_id: str, sr: StepResult) -> None:
        if not live:
            return
        try:
            self.qa_store.append_step(run_id, asdict(sr))
        except Exception as e:  # noqa: BLE001 — 落庫失敗不中斷執行
            print(f"[device] 逐步落庫失敗：{e}")
