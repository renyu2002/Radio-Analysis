"""Chrome 扩展（MV3）验收测试：静态检查 + mock 后端 + Chrome headless 行为验证。

覆盖 docs/CONTRACT.md §11 / §12.7：
1. ``manifest.json`` 可解析、MV3、权限最小化（activeTab/scripting/storage/tabs）、
   host_permissions 仅本地服务、图标尺寸正确、无 service worker；
2. popup 资源与 CSP 友好性（无内联脚本/事件、无外部资源、shared.js 先于 popup.js）；
3. popup.js / shared.js 静态约束（无 eval、无外网地址、tab 请求体构造、已知站点）；
4. mock 后端契约：GET /api/health、CORS 预检、POST /api/jobs 请求体字段（§8.3）；
5. Chrome headless 真实执行 ``shared.js``（``detectVideosInPage`` / ``buildTabJobPayload``
   / 真实 fetch 到 mock 后端），结果 JSON 零失败；
6. Chrome headless 加载 ``dev/preview.html``，验证 popup 四种状态（detected / offline /
   novideo / success）的 DOM 文案与禁用态。

所有产物写入 ``tests/.artifacts/extension/``（截图 + 结果 JSON），**不修改 extension/**；
mock 后端使用随机端口，不占用 8765/8767。
"""

from __future__ import annotations

import html as html_module
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from helpers import ROOT, chrome_path

EXT_DIR = ROOT / "extension"
ARTIFACTS = ROOT / "tests" / ".artifacts" / "extension"
MOCK_BACKEND_PATH = EXT_DIR / "dev" / "mock_backend.py"
BROWSER_TEST_PAGE = EXT_DIR / "dev" / "browser_test.html"
PREVIEW_PAGE = EXT_DIR / "dev" / "preview.html"

pytestmark = pytest.mark.extension

REQUIRED_PERMISSIONS = {"activeTab", "scripting", "storage", "tabs"}
EXPECTED_HOST_PERMISSIONS = ["http://127.0.0.1:8765/*"]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _artifacts_dir() -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS


def _load_mock_backend():
    """按文件路径加载 extension/dev/mock_backend.py（不写入扩展目录）。"""
    if not MOCK_BACKEND_PATH.is_file():
        pytest.skip("extension/dev/mock_backend.py 不存在")
    spec = importlib.util.spec_from_file_location("_va_ext_mock_backend", MOCK_BACKEND_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _chrome() -> Path:
    chrome = chrome_path()
    if chrome is None:
        pytest.skip("未找到 Chrome，跳过扩展浏览器行为测试")
    return chrome


def _chrome_flags(user_data_dir: Path) -> List[str]:
    return [
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--no-proxy-server",
        "--force-device-scale-factor=1",
        "--force-prefers-reduced-motion",
        f"--user-data-dir={user_data_dir}",
        "--window-size=1440,900",
    ]


def _dump_dom(url: str, *, budget_ms: int = 20000) -> str:
    chrome = _chrome()
    user_data_dir = Path(tempfile.mkdtemp(prefix="va_ext_chrome_"))
    try:
        result = subprocess.run(
            [
                str(chrome),
                *_chrome_flags(user_data_dir),
                f"--virtual-time-budget={budget_ms}",
                "--dump-dom",
                url,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        return result.stdout or ""
    finally:
        import shutil

        shutil.rmtree(user_data_dir, ignore_errors=True)


def _screenshot(url: str, out_path: Path, *, width: int = 1440, height: int = 900) -> bool:
    chrome = _chrome()
    user_data_dir = Path(tempfile.mkdtemp(prefix="va_ext_chrome_"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
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
                f"--user-data-dir={user_data_dir}",
                f"--window-size={width},{height}",
                "--virtual-time-budget=20000",
                f"--screenshot={out_path}",
                url,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        return out_path.is_file() and out_path.stat().st_size > 5000
    finally:
        import shutil

        shutil.rmtree(user_data_dir, ignore_errors=True)


def _extract_pre_json(dom: str, element_id: str) -> Optional[Dict[str, Any]]:
    """从 dump-dom 中提取 ``<pre id="...">{json}</pre>`` 的 JSON。"""
    match = re.search(
        rf'<pre[^>]*id="{re.escape(element_id)}"[^>]*>(.*?)</pre>',
        dom,
        flags=re.DOTALL,
    )
    if not match:
        return None
    text = html_module.unescape(match.group(1)).strip()
    if not text or text == "pending":
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _wait_report(url: str, element_id: str, *, budget_ms: int = 20000) -> Dict[str, Any]:
    dom = _dump_dom(url, budget_ms=budget_ms)
    data = _extract_pre_json(dom, element_id)
    assert data is not None, f"未能从 {url} 读取测试结果（DOM 长度 {len(dom)}）"
    return data


# ---------------------------------------------------------------------------
# 1) manifest / 权限 / 图标 / 资源静态检查
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def manifest() -> Dict[str, Any]:
    path = EXT_DIR / "manifest.json"
    assert path.is_file(), "extension/manifest.json 不存在"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_extension_manifest_minimal_permissions(manifest) -> None:
    assert manifest.get("manifest_version") == 3
    assert isinstance(manifest.get("name"), str) and manifest["name"].strip()
    assert isinstance(manifest.get("version"), str) and manifest["version"].strip()
    assert set(manifest.get("permissions") or []) == REQUIRED_PERMISSIONS
    assert list(manifest.get("host_permissions") or []) == EXPECTED_HOST_PERMISSIONS
    raw = json.dumps(manifest, ensure_ascii=False)
    assert "<all_urls>" not in raw
    assert "background" not in manifest, "契约建议不声明 service worker"
    assert ("action" in manifest) and manifest["action"].get("default_popup") == "popup.html"
    assert "content_scripts" not in manifest or manifest["content_scripts"] == [], (
        "应使用 activeTab + scripting.executeScript，而不是常驻 content script"
    )
    # 已知站点不通过额外 host 权限实现（契约 §11：activeTab+scripting 足够）
    assert not any("bilibili" in item or "youtube" in item for item in manifest.get("host_permissions") or [])


def test_extension_icons_exist_and_sized(manifest) -> None:
    from PIL import Image

    icons = manifest.get("icons") or {}
    default_icon = (manifest.get("action") or {}).get("default_icon") or {}
    assert {int(key) for key in icons} == {16, 32, 48, 128}
    for size in (16, 32, 48, 128):
        relative = icons.get(str(size)) or default_icon.get(str(size))
        assert relative, f"manifest 缺少 {size} 图标"
        icon_path = EXT_DIR / relative
        assert icon_path.is_file(), f"图标不存在：{relative}"
        with Image.open(icon_path) as image:
            assert image.size == (size, size), f"图标 {relative} 尺寸 {image.size} != {(size, size)}"
            assert image.format == "PNG"
    assert (EXT_DIR / "make_icons.py").is_file()
    assert not (EXT_DIR / "node_modules").exists()


def test_extension_popup_resources_and_csp(manifest) -> None:
    popup = (EXT_DIR / "popup.html").read_text(encoding="utf-8")
    for asset in ("popup.css", "shared.js", "popup.js"):
        assert (EXT_DIR / asset).is_file(), f"缺少 popup 资源 {asset}"
    assert "分析这个视频" in popup
    assert "打开分析界面" in popup
    assert "当前页面" in popup and "本地服务" in popup
    assert "重试" in popup and "本地服务地址" in popup
    # shared.js 必须先于 popup.js
    shared_pos = popup.find('src="shared.js"')
    popup_pos = popup.find('src="popup.js"')
    assert 0 <= shared_pos < popup_pos, "popup.html 应先加载 shared.js 再加载 popup.js"
    # MV3 CSP：无内联脚本 / 内联事件 / 外部资源
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", popup), "popup.html 不应有内联 <script>"
    assert not re.search(r"\son[a-z]+\s*=", popup, re.IGNORECASE), "popup.html 不应有内联事件属性"
    assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', popup, re.IGNORECASE), (
        "popup.html 不应引用外部 http(s) 资源（离线可用）"
    )


def test_extension_js_static_rules() -> None:
    shared = (EXT_DIR / "shared.js").read_text(encoding="utf-8")
    popup = (EXT_DIR / "popup.js").read_text(encoding="utf-8")
    for name, source in (("shared.js", shared), ("popup.js", popup)):
        assert "eval(" not in source, f"{name} 不应使用 eval"
        assert "new Function" not in source, f"{name} 不应使用 new Function"
        assert "<all_urls>" not in source
        external = re.search(
            r"https?://(?!127\.0\.0\.1(?:[:/]|$))[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", source
        )
        assert not external, f"{name} 不应请求外部网络地址：{external.group(0) if external else ''}"
    # shared.js：契约 §8.3 tab 请求体 + 视频检测 + 后端调用
    for token in (
        "function buildTabJobPayload",
        "function detectVideosInPage",
        "function submitTabJob",
        "function backendHealth",
        "function normalizeBackendUrl",
        "source_type: 'tab'",
        "video_url",
        "page_url",
        "currentSrc",
        "duration",
        "poster",
        "bilibili.com",
        "youtube.com",
        "youtu.be",
        "douyin.com",
        "vimeo.com",
    ):
        assert token in shared, f"shared.js 缺少契约要素：{token}"
    assert "http://127.0.0.1:8765" in shared  # 默认后端地址
    # popup.js：chrome API 使用 + 事件绑定
    for token in ("chrome.storage", "chrome.tabs.query", "chrome.scripting.executeScript", "addEventListener"):
        assert token in popup or token in shared, f"扩展未使用 {token}"
    assert "/api/jobs" in popup or "/api/jobs" in shared
    assert (EXT_DIR / "README.md").is_file()
    readme = (EXT_DIR / "README.md").read_text(encoding="utf-8")
    for token in ("chrome://extensions", "加载已解压的扩展程序", "127.0.0.1:8765", "权限"):
        assert token in readme, f"README 缺少安装说明：{token}"


# ---------------------------------------------------------------------------
# 2) mock 后端契约（不依赖 Chrome）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def mock_backend():
    module = _load_mock_backend()
    server, state = module.create_server(0)  # 随机端口，避免占用
    port = int(server.server_address[1])
    state.port = port
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        server.shutdown()
        server.server_close()


def test_mock_backend_health_and_cors(mock_backend) -> None:
    import httpx

    base, _state = mock_backend
    with httpx.Client(timeout=10.0) as client:
        health = client.get(f"{base}/api/health")
        assert health.status_code == 200
        data = health.json()
        assert data["ok"] is True
        assert {"version", "asr", "port"} <= set(data)
        preflight = client.options(
            f"{base}/api/jobs",
            headers={
                "Origin": "chrome-extension://abcdefghijklmnop",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert preflight.status_code == 204
        assert preflight.headers.get("access-control-allow-origin") == "*"
        assert "content-type" in (preflight.headers.get("access-control-allow-headers") or "").lower()


def test_mock_backend_rejects_invalid_tab_body(mock_backend) -> None:
    import httpx

    base, state = mock_backend
    before = len(state.jobs)
    with httpx.Client(timeout=10.0) as client:
        invalid = client.post(f"{base}/api/jobs", json={"source_type": "tab", "page_url": ""})
        assert invalid.status_code == 400
        assert isinstance(invalid.json().get("detail"), str)
    assert len(state.jobs) == before, "非法请求不应被记录为有效作业"


# ---------------------------------------------------------------------------
# 3) Chrome headless：shared.js 在真实浏览器中的行为（含真实 fetch 到 mock）
# ---------------------------------------------------------------------------
def test_extension_browser_logic_with_mock(mock_backend) -> None:
    base, state = mock_backend
    result = _wait_report(f"{base}/dev/browser_test.html", "result")
    _artifacts_dir().joinpath("browser_test.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    assert result.get("kind") == "browser-test"
    assert result.get("failed") == 0, [
        check for check in result.get("checks") or [] if not check.get("ok")
    ]
    check_map = {check["name"]: check["ok"] for check in result.get("checks") or []}
    for name in (
        "请求体字段与契约 §8.3 一致",
        "source_type 为 tab",
        "video_url 原样传递 blob 地址",
        "duration 为数字类型",
        "识别 blob: 视频",
        "优先选择正在播放的可见视频",
        "递归扫描同源 iframe",
        "GET /api/health 成功",
        "POST /api/jobs 返回 job",
        "后端未启动时优雅失败",
    ):
        assert check_map.get(name) is True, f"浏览器行为断言未通过：{name}"

    # mock 后端应收到一次契约结构的 tab 请求体
    assert len(state.jobs) == 1, f"mock 后端收到 {len(state.jobs)} 次 POST /api/jobs"
    record = state.jobs[0]
    body = record["json"]
    assert set(body) == {"source_type", "page_url", "video_url", "title", "duration", "poster"}
    assert body["source_type"] == "tab"
    assert body["page_url"].startswith("https://www.bilibili.com/video/")
    assert str(body["video_url"]).startswith("blob:")
    assert isinstance(body["duration"], (int, float))
    headers = {str(key).lower(): str(value) for key, value in record["headers"].items()}
    assert "application/json" in (headers.get("content-type") or "")
    assert (headers.get("referer") or "").startswith(base)


# ---------------------------------------------------------------------------
# 4) Chrome headless：popup 四种状态的 DOM 文案与禁用态
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["detected", "offline", "novideo", "success"])
def test_extension_popup_states(mock_backend, mode: str) -> None:
    base, _state = mock_backend
    result = _wait_report(f"{base}/dev/preview.html?mode={mode}", "report")
    _artifacts_dir().joinpath(f"popup_{mode}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    assert result.get("kind") == "popup-page"
    snapshot = result.get("snapshot") or {}
    assert snapshot, f"popup 状态 {mode} 未产生快照：{result.get('error')}"
    assert not result.get("error"), result.get("error")
    assert snapshot.get("bodyWidth", 0) > 300, f"popup 宽度异常：{snapshot.get('bodyWidth')}"

    if mode == "detected":
        assert "演示视频标题" in snapshot.get("pageTitle", "")
        assert "bilibili.com" in snapshot.get("pageUrl", "")
        assert "检测到视频" in snapshot.get("detectTitle", "")
        assert snapshot.get("duration") == "12:34", snapshot.get("duration")
        assert "已连接" in snapshot.get("backendText", "")
        assert snapshot.get("analyzeDisabled") is False
        assert snapshot.get("analyzeHidden") is False
        assert snapshot.get("errorHidden") is True
    elif mode == "offline":
        assert "未启动" in snapshot.get("backendText", "") or "未连接" in snapshot.get("backendText", "")
        assert snapshot.get("analyzeDisabled") is True
        assert "重试" in snapshot.get("html", "")
    elif mode == "novideo":
        assert "未检测到" in snapshot.get("detectTitle", "")
        # 已知视频站点但未检测到 <video>（常见于跨域 iframe）时，允许降级为
        # 「按页面链接分析」让后端 yt-dlp 解析页面，因此按钮是可用的。
        assert snapshot.get("analyzeDisabled") is False
        assert snapshot.get("actionsLabel") in ("按页面链接分析", "分析这个视频")
        assert "页面链接" in snapshot.get("html", "") or "页面链接" in snapshot.get("detectHint", "")
    elif mode == "success":
        assert snapshot.get("successHidden") is False, "提交成功后应显示成功卡片"
        assert "演示视频标题" in snapshot.get("successTitle", "") or "排队" in snapshot.get("successSub", "")
        assert "打开分析界面" in snapshot.get("html", "")


def test_extension_popup_screenshot(mock_backend) -> None:
    """保存 popup detected 状态截图（人工/视觉验收用）。"""
    base, _state = mock_backend
    out_path = _artifacts_dir() / "popup_detected.png"
    ok = _screenshot(f"{base}/dev/preview.html?mode=detected", out_path, width=420, height=720)
    assert ok, f"popup 截图生成失败：{out_path}"
    from PIL import Image

    with Image.open(out_path) as image:
        assert image.size == (420, 720)
