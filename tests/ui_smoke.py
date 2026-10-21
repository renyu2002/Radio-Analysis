"""UI 冒烟验收：Chrome headless 截图 + DOM 关键元素检查（不依赖 pytest）。

验收内容（docs/CONTRACT.md §12.5）
---------------------------------
1. 起真实服务（默认 127.0.0.1:8799，不占用 8765），数据目录用临时目录；
2. 预置一个已完成的作业（DB + 产物），使作业详情页有完整内容可渲染；
3. Chrome headless 截图首页与作业详情页 → ``tests/.artifacts/home.png`` /
   ``detail.png``（1440x900）；
4. ``--dump-dom`` 检查关键元素/文案：产品名、新建分析、历史作业、设置、拖拽区、
   详情页的作业标题 + 四个标签页 + 播放器；
5. 输出 ``tests/.artifacts/ui_smoke.json`` 结果清单；全部必需项通过则退出码 0。

用法
----
::

    .venv\\Scripts\\python.exe tests\\ui_smoke.py
    $env:UI_SMOKE_PORT="8799"; .venv\\Scripts\\python.exe tests\\ui_smoke.py

环境变量
--------
- ``UI_SMOKE_PORT``（默认 8799）
- ``UI_KEEP_DATA=1`` 保留临时数据目录（调试用）
- ``CHROME_PATH`` 显式指定 chrome.exe
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
ARTIFACTS = TESTS_DIR / ".artifacts"
# 直接运行 `python tests/ui_smoke.py` 时 sys.path[0] 是 tests/，需要项目根目录才能 import vanalyze
for _path in (ROOT, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# ---------------------------------------------------------------------------
# 环境隔离：必须在导入 vanalyze 之前把数据目录指向临时目录
# ---------------------------------------------------------------------------
_DATA_DIR = Path(tempfile.mkdtemp(prefix="vanalyze_ui_smoke_"))
os.environ["VANALYZE_DATA_DIR"] = str(_DATA_DIR)
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.pop("DEEPSEEK_API_KEY", None)

from helpers import VENV_PYTHON, chrome_path, seed_job, wait_http_health  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("UI_SMOKE_PORT", "8799"))
BASE_URL = f"http://{HOST}:{PORT}"
WINDOW = (1440, 900)
SERVER_LOG = ARTIFACTS / "ui_smoke_server.log"
FAKE_KEY = "sk-ui-smoke-fake-key-123456"
JOB_TITLE = "UI 冒烟测试视频"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _port_free() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, PORT))
        except OSError:
            return False
    return True


def _find_chrome() -> Path:
    override = os.environ.get("CHROME_PATH", "").strip()
    if override:
        path = Path(override)
        if path.is_file():
            return path
        raise SystemExit(f"CHROME_PATH 指定的 Chrome 不存在：{path}")
    chrome = chrome_path()
    if chrome is None:
        raise SystemExit("未找到 Chrome，请安装 Chrome 或设置 CHROME_PATH 环境变量")
    return chrome


def _chrome_flags(user_data_dir: Path) -> List[str]:
    flags = [
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--no-proxy-server",
        "--force-device-scale-factor=1",
        # 关闭/压缩 CSS 动画：headless 截图会停在 fade-in 起始帧（列表发灰），
        # 触发站点的 prefers-reduced-motion 规则让截图稳定在最终态。
        "--force-prefers-reduced-motion",
        "--autoplay-policy=no-user-gesture-required",
        "--mute-audio",
        # 等待所有合成阶段完成再截图（否则偶发丢失 <video> 原生控件绘制）
        "--run-all-compositor-stages-before-draw",
        f"--user-data-dir={user_data_dir}",
        f"--window-size={WINDOW[0]},{WINDOW[1]}",
    ]
    extra = os.environ.get("UI_CHROME_FLAGS", "").strip()
    if extra:
        flags.extend(extra.split())
    return flags


def _chrome_screenshot(chrome: Path, url: str, out_path: Path, user_data_dir: Path) -> Tuple[bool, str]:
    """Chrome headless 截图，返回 (是否成功, 说明)。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(chrome),
        *_chrome_flags(user_data_dir),
        "--virtual-time-budget=15000",
        f"--screenshot={out_path}",
        url,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=120,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, "Chrome 截图超时（120s）"
    if not out_path.is_file():
        stderr = (result.stderr or "")[-800:]
        return False, f"Chrome 未生成截图（exit={result.returncode}）：{stderr}"
    size = out_path.stat().st_size
    if size < 5_000:
        return False, f"截图文件过小（{size} bytes），页面可能未渲染"
    return True, f"{out_path.name}（{size // 1024} KB）"


def _chrome_dump_dom(chrome: Path, url: str, user_data_dir: Path) -> Tuple[str, str]:
    """Chrome headless --dump-dom，返回 (DOM 文本, 错误说明)。"""
    command = [
        str(chrome),
        *_chrome_flags(user_data_dir),
        "--virtual-time-budget=15000",
        "--dump-dom",
        url,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=120,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return "", "Chrome --dump-dom 超时（120s）"
    dom = result.stdout or ""
    if not dom.strip():
        return "", f"DOM 为空（exit={result.returncode}）：{(result.stderr or '')[-500:]}"
    return dom, ""


def _png_size(path: Path) -> Optional[Tuple[int, int]]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 服务启动
# ---------------------------------------------------------------------------
def _start_server() -> Tuple[subprocess.Popen, Any]:
    if not _port_free():
        raise SystemExit(
            f"端口 {PORT} 已被占用，无法启动 UI 冒烟服务（可能有残留进程）。"
            f"可设置 UI_SMOKE_PORT 换端口。"
        )
    if not VENV_PYTHON.is_file():
        raise SystemExit(f"找不到 venv Python：{VENV_PYTHON}")
    env = os.environ.copy()
    env.update(
        {
            "VANALYZE_DATA_DIR": str(_DATA_DIR),
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "DEEPSEEK_API_KEY": FAKE_KEY,
            "DEEPSEEK_BASE_URL": "http://127.0.0.1:8777",
            "VANALYZE_WHISPER_MODEL": "tiny",
        }
    )
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    log_handle = SERVER_LOG.open("w", encoding="utf-8", errors="replace")
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):  # pragma: no cover - Windows only
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    process = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "vanalyze", "--host", HOST, "--port", str(PORT)],
        cwd=str(ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    return process, log_handle


# ---------------------------------------------------------------------------
# 检查项
# ---------------------------------------------------------------------------
def _check_contains(dom: str, needles: Dict[str, str]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for key, needle in needles.items():
        ok = needle in dom
        results.append({"item": key, "expected": f"DOM 含「{needle}」", "ok": ok})
    return results


def _detail_route_candidates(job_id: str) -> List[str]:
    """猜测 SPA 的作业详情路由（ui-dev 最终实现后由 DOM 验证挑选）。"""
    return [
        f"{BASE_URL}/#/job/{job_id}",
        f"{BASE_URL}/#job/{job_id}",
        f"{BASE_URL}/job/{job_id}",
        f"{BASE_URL}/#/jobs/{job_id}",
        f"{BASE_URL}/?job={job_id}",
    ]


def _score_detail_dom(dom: str) -> int:
    markers = [JOB_TITLE, "总结", "转写", "画面文字", "对话", 'id="video"', 'id="tab-summary"']
    return sum(1 for marker in markers if marker in dom)


def _pick_detail_route(chrome: Path, user_data_dir: Path, job_id: str) -> Tuple[Optional[str], str, List[str]]:
    """尝试候选路由，返回 (可用 URL, DOM, 尝试记录)。"""
    attempts: List[str] = []
    best: Tuple[int, Optional[str], str] = (-1, None, "")
    for url in _detail_route_candidates(job_id):
        dom, error = _chrome_dump_dom(chrome, url, user_data_dir)
        if error:
            attempts.append(f"{url} → 失败：{error}")
            continue
        score = _score_detail_dom(dom)
        attempts.append(f"{url} → 命中 {score}/7")
        if score > best[0]:
            best = (score, url, dom)
        if JOB_TITLE in dom and "总结" in dom and 'id="video"' in dom:
            return url, dom, attempts
    return best[1], best[2], attempts


# ---------------------------------------------------------------------------
# CDP 交互截图（点击标签页/设置弹窗；可选增强，失败不影响主验收）
# ---------------------------------------------------------------------------
def _cdp_interaction_screenshots(
    chrome: Path, detail_url: str, job_id: str
) -> Dict[str, Any]:
    """用 Chrome DevTools Protocol 点击页面并截图（转写/画面文字/对话/设置弹窗）。

    返回 {"ok": bool, "shots": {...}, "error": str}；任何失败都只记录不抛出。
    """
    import base64
    import json as json_module
    import time as time_module

    result: Dict[str, Any] = {"ok": False, "shots": {}, "error": ""}
    try:
        from websockets.sync.client import connect
    except Exception as exc:  # noqa: BLE001 - websockets 不可用时跳过增强截图
        result["error"] = f"websockets 不可用：{exc}"
        return result

    profile = ARTIFACTS / ".cdp_profile"
    shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        str(chrome),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--no-proxy-server",
        "--force-device-scale-factor=1",
        "--force-prefers-reduced-motion",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        f"--window-size={WINDOW[0]},{WINDOW[1]}",
        "about:blank",
    ]
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):  # pragma: no cover - Windows only
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time_module.monotonic() + 30
        while time_module.monotonic() < deadline and not port_file.is_file():
            time_module.sleep(0.1)
        if not port_file.is_file():
            result["error"] = "Chrome 未写出 DevToolsActivePort"
            return result
        lines = port_file.read_text(encoding="utf-8", errors="replace").splitlines()
        port = int(lines[0].strip())
        ws_path = lines[1].strip()
        message_id = 0

        with connect(f"ws://127.0.0.1:{port}{ws_path}", open_timeout=30, max_size=None) as websocket:
            def _send(method: str, params: Optional[Dict[str, Any]] = None, session: Optional[str] = None) -> Dict[str, Any]:
                nonlocal message_id
                message_id += 1
                message: Dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
                if session:
                    message["sessionId"] = session
                websocket.send(json_module.dumps(message))
                while True:
                    response = json_module.loads(websocket.recv())
                    if response.get("id") == message_id:
                        return response

            target_id = _send("Target.createTarget", {"url": "about:blank"})["result"]["targetId"]
            session_id = _send(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )["result"]["sessionId"]
            _send("Page.enable", session=session_id)
            _send("Runtime.enable", session=session_id)
            _send(
                "Emulation.setDeviceMetricsOverride",
                {"width": WINDOW[0], "height": WINDOW[1], "deviceScaleFactor": 1, "mobile": False},
                session=session_id,
            )
            _send("Page.navigate", {"url": detail_url}, session=session_id)

            def _evaluate(expression: str) -> Any:
                response = _send(
                    "Runtime.evaluate",
                    {"expression": expression, "awaitPromise": True, "returnByValue": True},
                    session=session_id,
                )
                return (response.get("result") or {}).get("result", {}).get("value")

            def _wait_for(expression: str, timeout: float = 20.0) -> bool:
                end = time_module.monotonic() + timeout
                while time_module.monotonic() < end:
                    try:
                        if _evaluate(expression):
                            return True
                    except Exception:  # noqa: BLE001 - 页面切换瞬间可能报错
                        pass
                    time_module.sleep(0.2)
                return False

            if not _wait_for(f"document.querySelector('#jobTitle') && document.querySelector('#jobTitle').textContent.includes('{JOB_TITLE}')", 40):
                result["error"] = "详情页未渲染作业标题"
                return result
            time_module.sleep(1.0)

            def _shot(name: str, label: str) -> None:
                response = _send("Page.captureScreenshot", {"format": "png"}, session=session_id)
                data = base64.b64decode(response["result"]["data"])
                out_path = ARTIFACTS / f"{name}.png"
                out_path.write_bytes(data)
                result["shots"][label] = {"path": str(out_path), "bytes": len(data)}

            _shot("detail_summary", "总结")
            _evaluate("window.scrollTo(0,0); true")

            if _evaluate("""(function(){var el=document.querySelector('[data-tab="transcript"]'); if(el){el.click(); return true;} return false;})()"""):
                _wait_for("!document.getElementById('tab-transcript').classList.contains('is-hidden') && document.getElementById('tab-transcript').textContent.length > 10", 15)
                time_module.sleep(0.8)
                _shot("detail_transcript", "转写")

            if _evaluate("""(function(){var el=document.querySelector('[data-tab="ocr"]'); if(el){el.click(); return true;} return false;})()"""):
                _wait_for("!document.getElementById('tab-ocr').classList.contains('is-hidden') && document.getElementById('tab-ocr').querySelectorAll('img').length > 0", 15)
                time_module.sleep(0.8)
                _shot("detail_ocr", "画面文字")

            if _evaluate("""(function(){var el=document.querySelector('[data-tab="chat"]'); if(el){el.click(); return true;} return false;})()"""):
                _wait_for("!document.getElementById('tab-chat').classList.contains('is-hidden')", 10)
                time_module.sleep(0.8)
                _shot("detail_chat", "对话")

            if _evaluate("""(function(){var el=document.getElementById('btnSettings'); if(el){el.click(); return true;} return false;})()"""):
                _wait_for("!document.getElementById('settingsModal').classList.contains('is-hidden')", 10)
                _wait_for("(document.getElementById('setBaseUrl')||{}).value", 10)
                time_module.sleep(0.8)
                _shot("settings_modal", "设置弹窗")

            _send("Target.closeTarget", {"targetId": target_id})
        result["ok"] = bool(result["shots"])
        return result
    except Exception as exc:  # noqa: BLE001 - 增强截图失败不影响主验收
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        try:
            process.terminate()
            process.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    chrome = _find_chrome()
    if not (ROOT / "web" / "index.html").is_file():
        raise SystemExit("web/index.html 不存在，前端尚未就绪")
    if not (ROOT / "web" / "app.js").is_file():
        print("[WARN] web/app.js 尚不存在：详情页交互无法渲染（ui-dev 完成后重跑）")

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    user_data_dir = Path(tempfile.mkdtemp(prefix="vanalyze_chrome_"))
    report: Dict[str, Any] = {
        "chrome": str(chrome),
        "base_url": BASE_URL,
        "window": list(WINDOW),
        "notes": [
            "Chrome headless 无法把 <video> 解码画面合成进截图（实测纯本地 <video> 页面截图同样全黑），"
            "播放器以 DOM 结构 + 媒体端点 + HTTP Range 206 测试为准。",
        ],
        "checks": [],
    }
    server_process = None
    log_handle = None
    detail_url = ""

    try:
        # 预置一个已完成作业，保证详情页有完整数据
        job_id = seed_job(title=JOB_TITLE, with_frames=3)
        print(f"[UI] 已预置作业：{job_id}（{JOB_TITLE}）")
        report["job_id"] = job_id

        server_process, log_handle = _start_server()
        health = wait_http_health(BASE_URL, timeout=120)
        print(f"[UI] 服务已就绪：{BASE_URL}（health.ok={health.get('ok')}）")
        report["health"] = health

        # ---------- 首页 ----------
        home_png = ARTIFACTS / "home.png"
        home_ok, home_note = _chrome_screenshot(chrome, f"{BASE_URL}/", home_png, user_data_dir)
        home_dom, home_error = _chrome_dump_dom(chrome, f"{BASE_URL}/", user_data_dir)
        (ARTIFACTS / "home.html").write_text(home_dom, encoding="utf-8")
        report["home"] = {"screenshot": str(home_png), "screenshot_ok": home_ok, "note": home_note}
        report["checks"].append({"item": "首页截图", "expected": "生成 home.png", "ok": home_ok, "detail": home_note})
        if home_dom:
            size = _png_size(home_png) if home_ok else None
            report["home"]["png_size"] = list(size) if size else None
            report["checks"].append(
                {
                    "item": "首页截图尺寸",
                    "expected": "1440x900",
                    "ok": bool(size) and tuple(size) == WINDOW,
                    "detail": str(size),
                }
            )
            report["checks"].extend(
                _check_contains(
                    home_dom,
                    {
                        "产品名": "视频分析",
                        "DeepSeek 品牌": "DeepSeek",
                        "新建分析面板": "新建分析",
                        "历史作业面板": "历史作业",
                        "历史作业列表项": JOB_TITLE,
                        "设置按钮": "设置",
                        "拖拽上传区": 'id="dropzone"',
                        "设置弹窗": 'id="settingsModal"',
                        "空状态引导": "看懂每一个视频",
                    },
                )
            )
        else:
            report["checks"].append(
                {"item": "首页 DOM", "expected": "非空 DOM", "ok": False, "detail": home_error}
            )

        # ---------- 作业详情页 ----------
        detail_url, detail_dom, attempts = _pick_detail_route(chrome, user_data_dir, job_id)
        report["detail_attempts"] = attempts
        if detail_dom:
            (ARTIFACTS / "detail.html").write_text(detail_dom, encoding="utf-8")
        report["detail_url"] = detail_url or ""
        if detail_url and detail_dom:
            detail_png = ARTIFACTS / "detail.png"
            detail_ok, detail_note = _chrome_screenshot(chrome, detail_url, detail_png, user_data_dir)
            size = _png_size(detail_png) if detail_ok else None
            report["detail"] = {
                "url": detail_url,
                "screenshot": str(detail_png),
                "screenshot_ok": detail_ok,
                "note": detail_note,
                "png_size": list(size) if size else None,
                "attempts": attempts,
            }
            report["checks"].append(
                {"item": "详情页截图", "expected": "生成 detail.png", "ok": detail_ok, "detail": detail_note}
            )
            report["checks"].append(
                {
                    "item": "详情页截图尺寸",
                    "expected": "1440x900",
                    "ok": bool(size) and tuple(size) == WINDOW,
                    "detail": str(size),
                }
            )
            report["checks"].extend(
                _check_contains(
                    detail_dom,
                    {
                        "作业标题": JOB_TITLE,
                        "总结标签": 'id="tab-summary"',
                        "转写标签": 'id="tab-transcript"',
                        "画面文字标签": 'id="tab-ocr"',
                        "对话标签": 'id="tab-chat"',
                        "播放器": 'id="video"',
                        "格式清晰的页面标题": "视频分析",
                    },
                )
            )
            # 播放器 src 指向后端媒体端点（<video> 可 seek 由 API Range 测试覆盖）
            report["checks"].append(
                {
                    "item": "播放器媒体地址",
                    "expected": f"video[src] 含 /api/jobs/{job_id}/media，且有缩略图 poster",
                    "ok": (f'/api/jobs/{job_id}/media' in detail_dom)
                    and (f'/api/jobs/{job_id}/thumbnail' in detail_dom),
                }
            )
            # 总结标签页应已渲染（一句话结论 / 章节 / 要点）
            report["checks"].append(
                {
                    "item": "总结内容渲染",
                    "expected": "DOM 含「一句话结论」「章节」「要点」",
                    "ok": all(marker in detail_dom for marker in ("一句话结论", "章节", "要点")),
                }
            )
            # 无错误横幅、转写数量正确
            report["checks"].append(
                {
                    "item": "错误横幅隐藏",
                    "expected": "error-banner is-hidden",
                    "ok": "error-banner is-hidden" in detail_dom or 'id="errorBanner"' not in detail_dom,
                }
            )
            report["checks"].append(
                {
                    "item": "转写数量",
                    "expected": "transcriptTabCount = 3（种子作业 3 段）",
                    "ok": 'id="transcriptTabCount">3' in detail_dom,
                    "detail": re.findall(r'id="transcriptTabCount"[^>]*>([^<]*)', detail_dom)[:2],
                }
            )
            # 详情页应渲染作业状态（完成）
            report["checks"].append(
                {
                    "item": "作业状态徽章",
                    "expected": "DOM 含「完成」或 jobStatusBadge",
                    "ok": ("完成" in detail_dom) or ('id="jobStatusBadge"' in detail_dom),
                }
            )
        else:
            report["checks"].append(
                {
                    "item": "详情页路由",
                    "expected": "能打开作业详情路由",
                    "ok": False,
                    "detail": f"候选路由均未渲染作业标题：{attempts}",
                }
            )

        # ---------- CDP 交互截图（点击标签页/设置弹窗；可选增强，不影响主验收） ----------
        if detail_url:
            interaction = _cdp_interaction_screenshots(chrome, detail_url, job_id)
            report["interaction_screenshots"] = interaction
            if interaction.get("ok"):
                for label, info in (interaction.get("shots") or {}).items():
                    print(f"[UI] 交互截图（{label}）：{info['path']}（{info['bytes'] // 1024} KB）")
            else:
                print(f"[WARN] CDP 交互截图失败（不影响主验收）：{interaction.get('error')}")

        # ---------- 汇总 ----------
        required = [check for check in report["checks"]]
        failed = [check for check in required if not check["ok"]]
        report["passed"] = len(required) - len(failed)
        report["failed"] = len(failed)
        report["success"] = not failed
        (ARTIFACTS / "ui_smoke.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        print("\n" + "=" * 72)
        print(f"UI 冒烟结果：{report['passed']} 通过 / {len(failed)} 失败")
        for check in required:
            mark = "PASS" if check["ok"] else "FAIL"
            detail = check.get("detail") or check.get("expected") or ""
            print(f"  [{mark}] {check['item']}：{detail if not check['ok'] else check['expected']}")
        print(f"截图：{ARTIFACTS / 'home.png'}")
        print(f"      {ARTIFACTS / 'detail.png' if detail_url else '（详情页未生成）'}")
        for label, info in (report.get("interaction_screenshots", {}).get("shots") or {}).items():
            print(f"      {info['path']}（{label}）")
        print(f"结果：{ARTIFACTS / 'ui_smoke.json'}")
        print("=" * 72)
        return 0 if report["success"] else 1
    finally:
        if server_process is not None:
            try:
                server_process.terminate()
                server_process.wait(timeout=15)
            except Exception:  # noqa: BLE001
                try:
                    server_process.kill()
                    server_process.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        if log_handle is not None:
            log_handle.close()
        shutil.rmtree(user_data_dir, ignore_errors=True)
        if os.environ.get("UI_KEEP_DATA", "") not in ("1", "true", "yes"):
            shutil.rmtree(_DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
