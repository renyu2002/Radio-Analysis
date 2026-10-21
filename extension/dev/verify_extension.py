"""视频分析 · DeepSeek —— Chrome 扩展自测脚本。

覆盖：
  1. 静态检查：manifest.json 结构/权限最小化、图标文件与尺寸、popup 资源与 CSP 友好性；
  2. mock 后端（127.0.0.1:8767）：GET /api/health、POST /api/jobs 请求体契约、CORS 预检、错误分支；
  3. Chrome headless：加载 dev/browser_test.html，在真实浏览器里执行 shared.js 的
     请求体构造 / 视频检测（含同源 iframe 递归）/ 真实 fetch 到 mock，并断言结果；
  4. Chrome headless：dev/preview.html 注入 chrome API 打桩，断言 popup 各状态 DOM 文案；
  5. Chrome headless 截图：file:// 直接打开 popup.html + 三种 popup 状态样式。

用法（项目根目录）：
    .\\\\.venv\\\\Scripts\\\\python.exe extension\\\\dev\\\\verify_extension.py

不占用真实后端端口 8765；mock 使用 8767。测试结束自动关闭 mock 与临时 Chrome profile。
"""

from __future__ import annotations

import html as html_module
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent
EXT_DIR = DEV_DIR.parent

sys.path.insert(0, str(DEV_DIR))

import httpx  # noqa: E402  （项目 venv 已随后端安装）

from mock_backend import create_server  # noqa: E402

MOCK_PORT = 8767
BASE = f"http://127.0.0.1:{MOCK_PORT}"
SHOTS_DIR = DEV_DIR / "screenshots"
CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)
PAGE_URL = "https://www.bilibili.com/video/BV1xx411c7mD"
PAGE_TITLE = "【4K】视频分析扩展 · 演示视频标题"

RESULTS: list[dict] = []


# --------------------------------------------------------------------------- #
# 断言与输出
# --------------------------------------------------------------------------- #

def check(name: str, ok: bool, detail=None) -> None:
    RESULTS.append({
        "name": name,
        "ok": bool(ok),
        "detail": None if detail is None else str(detail)[:800],
    })
    tag = "PASS" if ok else "FAIL"
    suffix = "" if ok or detail is None else f"  -> {detail!r}"
    print(f"[{tag}] {name}{suffix}", flush=True)


def expect_equals(name: str, actual, expected) -> None:
    check(name, actual == expected, f"actual={actual!r} expected={expected!r}")


def expect_contains(name: str, actual, needle: str) -> None:
    check(name, isinstance(actual, str) and needle in actual, f"actual={actual!r}")


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


# --------------------------------------------------------------------------- #
# 静态检查
# --------------------------------------------------------------------------- #

def static_checks() -> None:
    section("1. 静态检查：manifest / 图标 / 页面资源")
    manifest_path = EXT_DIR / "manifest.json"
    check("manifest.json 存在", manifest_path.is_file())
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        check("manifest.json 是合法 JSON", False, exc)
        return
    check("manifest.json 是合法 JSON", True)

    expect_equals("manifest_version 为 3", manifest.get("manifest_version"), 3)
    for key in ("name", "description", "version"):
        value = manifest.get(key)
        check(f"manifest.{key} 为非空字符串", isinstance(value, str) and bool(value.strip()), value)

    permissions = set(manifest.get("permissions") or [])
    expected_permissions = {"activeTab", "scripting", "storage", "tabs"}
    expect_equals("permissions 精确为 activeTab/scripting/storage/tabs", permissions, expected_permissions)

    host_permissions = manifest.get("host_permissions") or []
    expect_equals("host_permissions 仅为本地服务 8765", host_permissions, ["http://127.0.0.1:8765/*"])
    check("未申请 <all_urls>", "<all_urls>" not in permissions and "<all_urls>" not in host_permissions)
    check("未声明 background service worker", "background" not in manifest)

    action = manifest.get("action") or {}
    expect_equals("action.default_popup 为 popup.html", action.get("default_popup"), "popup.html")
    popup_path = EXT_DIR / "popup.html"
    check("popup.html 存在", popup_path.is_file())

    expected_icon_sizes = {16, 32, 48, 128}
    icons = manifest.get("icons") or {}
    expect_equals("manifest.icons 含 16/32/48/128", {int(key) for key in icons}, expected_icon_sizes)
    try:
        from PIL import Image
        pillow_available = True
    except ImportError:
        pillow_available = False
        check("Pillow 可用（校验图标尺寸）", False)
    for size in sorted(expected_icon_sizes):
        relative = icons.get(str(size))
        icon_path = EXT_DIR / relative if relative else None
        check(f"图标 icon{size}.png 存在", bool(icon_path and icon_path.is_file()), relative)
        if pillow_available and icon_path and icon_path.is_file():
            try:
                with Image.open(icon_path) as image:
                    expect_equals(f"图标 {size} 尺寸正确", image.size, (size, size))
                    check(f"图标 {size} 为 RGBA PNG", image.format == "PNG" and image.mode == "RGBA", f"{image.format}/{image.mode}")
            except Exception as exc:  # noqa: BLE001 - 图标损坏时给出原因
                check(f"图标 {size} 可被 Pillow 读取", False, exc)

    if popup_path.is_file():
        page_html = popup_path.read_text(encoding="utf-8")
        for asset in ("popup.css", "shared.js", "popup.js"):
            check(f"popup 资源 {asset} 存在", (EXT_DIR / asset).is_file())
        shared_pos = page_html.find('src="shared.js"')
        popup_pos = page_html.find('src="popup.js"')
        check("popup.html 先加载 shared.js 再加载 popup.js", 0 <= shared_pos < popup_pos)
        check("popup.html 无内联 <script>（MV3 CSP）", not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page_html))
        check("popup.html 无内联事件属性", not re.search(r"\son[a-z]+\s*=", page_html, re.IGNORECASE))
        check("popup.html 无外部 http(s) 资源", not re.search(r'(?:src|href)\s*=\s*["\']https?://', page_html, re.IGNORECASE))

    js_checks = (("shared.js", EXT_DIR / "shared.js"), ("popup.js", EXT_DIR / "popup.js"))
    for name, js_path in js_checks:
        if not js_path.is_file():
            check(f"{name} 存在", False)
            continue
        source = js_path.read_text(encoding="utf-8")
        check(f"{name} 无 eval / new Function", "eval(" not in source and "new Function" not in source)
        check(
            f"{name} 无外部网络地址",
            not re.search(r"https?://(?!127\.0\.0\.1(?:[:/]|$))[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", source),
        )
        if name == "popup.js":
            check("popup.js 使用 addEventListener 绑定事件", "addEventListener" in source)
    shared_source = (EXT_DIR / "shared.js").read_text(encoding="utf-8")
    check("shared.js 定义 detectVideosInPage", "function detectVideosInPage" in shared_source)
    check("shared.js 构造 source_type: 'tab' 请求体", "source_type: 'tab'" in shared_source)

    css_path = EXT_DIR / "popup.css"
    if css_path.is_file():
        css = css_path.read_text(encoding="utf-8")
        check("popup.css 无外部 url() 资源", not re.search(r"url\(\s*['\"]?https?://", css, re.IGNORECASE))

    readme_path = EXT_DIR / "README.md"
    check("README.md 存在", readme_path.is_file())
    if readme_path.is_file():
        readme = readme_path.read_text(encoding="utf-8")
        expect_contains("README 含 chrome://extensions 安装路径", readme, "chrome://extensions")
        expect_contains("README 含开发者模式安装步骤", readme, "加载已解压的扩展程序")
        expect_contains("README 说明后端地址", readme, "127.0.0.1:8765")
        expect_contains("README 说明已知限制", readme, "限制")
    check("make_icons.py 存在", (EXT_DIR / "make_icons.py").is_file())
    check("扩展目录无 node_modules 等外部依赖", not (EXT_DIR / "node_modules").exists())


# --------------------------------------------------------------------------- #
# mock 后端契约检查
# --------------------------------------------------------------------------- #

def mock_contract_checks(state) -> None:
    section("2. mock 后端：健康检查 / CORS / POST /api/jobs 请求体契约")
    payload = {
        "source_type": "tab",
        "page_url": PAGE_URL,
        "video_url": "blob:https://www.bilibili.com/xyz-123",
        "title": "契约直连测试",
        "duration": 754.23,
        "poster": "https://i0.hdslb.com/bfs/archive/demo.jpg",
    }
    with httpx.Client(timeout=5.0) as client:
        response = client.get(BASE + "/api/health")
        health = response.json()
        expect_equals("GET /api/health 返回 200", response.status_code, 200)
        check("health.ok = true", health.get("ok") is True, health)
        check(
            "health 含 version/asr/port 字段",
            isinstance(health.get("version"), str)
            and isinstance(health.get("asr"), dict)
            and isinstance(health.get("port"), int),
            health,
        )

        preflight = client.options(
            BASE + "/api/jobs",
            headers={
                "Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        expect_equals("mock CORS 预检返回 204", preflight.status_code, 204)
        expect_equals("mock 允许扩展来源（ACAO *）", preflight.headers.get("access-control-allow-origin"), "*")
        check(
            "mock 允许 Content-Type 请求头",
            "content-type" in (preflight.headers.get("access-control-allow-headers") or "").lower(),
            preflight.headers.get("access-control-allow-headers"),
        )

        response = client.post(BASE + "/api/jobs", json=payload)
        body = response.json() if response.status_code == 200 else {}
        expect_equals("POST /api/jobs 返回 200", response.status_code, 200)
        check("响应含 job.id（契约 §8.3）", (body.get("job") or {}).get("id") == "mock-job-1", body)
        record = state.jobs[-1] if state.jobs else None
        check("mock 记录到 POST /api/jobs", record is not None)
        if record:
            sent = record["json"]
            expect_equals("mock 收到的请求体与发送一致", sent, payload)
            expect_equals(
                "tab 请求体字段集合符合契约 §8.3",
                set(sent),
                {"source_type", "page_url", "video_url", "title", "duration", "poster"},
            )
            check(
                "请求头 Content-Type 为 application/json",
                "application/json" in (record["headers"].get("Content-Type") or ""),
                record["headers"].get("Content-Type"),
            )
            check("duration 以数字传递", isinstance(sent.get("duration"), (int, float)), type(sent.get("duration")).__name__)

        payload_without_video = {
            "source_type": "tab",
            "page_url": "https://example.test/no-video",
            "video_url": None,
            "title": "无视频页面",
            "duration": 0,
            "poster": None,
        }
        response = client.post(BASE + "/api/jobs", json=payload_without_video)
        expect_equals("video_url/poster 为 null 的请求体被接受", response.status_code, 200)

        bad = client.post(BASE + "/api/jobs", json={"source_type": "tab", "page_url": ""})
        expect_equals("缺字段请求被 mock 拒绝（400）", bad.status_code, 400)
        check("错误响应为 {\"detail\": \"...\"} 结构", isinstance((bad.json() or {}).get("detail"), str), bad.text[:200])

        static_page = client.get(BASE + "/popup.html")
        check("mock 静态托管 popup.html", static_page.status_code == 200 and "视频分析" in static_page.text, static_page.status_code)


# --------------------------------------------------------------------------- #
# Chrome headless
# --------------------------------------------------------------------------- #

def find_chrome() -> Path | None:
    for candidate in CHROME_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def run_chrome(extra_args: list[str], timeout: int = 180):
    chrome = find_chrome()
    if chrome is None:
        return None
    profile = Path(tempfile.mkdtemp(prefix="va-ext-chrome-"))
    command = [
        str(chrome),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--hide-scrollbars",
        f"--user-data-dir={profile}",
        *extra_args,
    ]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def extract_json_element(page_html: str, element_id: str):
    if not page_html:
        return None
    match = re.search(
        rf'<pre id="{re.escape(element_id)}"[^>]*>(.*?)</pre>',
        page_html,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        return json.loads(html_module.unescape(match.group(1)))
    except json.JSONDecodeError:
        return None


def browser_logic_checks(state) -> None:
    section("3. Chrome headless：shared.js 逻辑 + 真实 fetch 到 mock")
    chrome = find_chrome()
    check("找到 Chrome 可执行文件", chrome is not None, chrome)
    if chrome is None:
        return

    page_url = f"{BASE}/dev/browser_test.html"
    process = run_chrome(["--dump-dom", "--virtual-time-budget=15000", page_url])
    page_html = process.stdout if process is not None else ""
    report = extract_json_element(page_html, "result")
    if report is None:
        check("浏览器逻辑测试页执行完成", False, "未解析到 #result，页面可能 JS 异常")
        debug_path = DEV_DIR / "debug-browser-test.html"
        debug_path.write_text(page_html or "", encoding="utf-8", errors="replace")
        print(f"  [debug] 已保存页面 DOM：{debug_path}", flush=True)
        if process is not None and process.stderr:
            print(f"  [chrome stderr] {process.stderr.strip()[-600:]}", flush=True)
        return

    check("浏览器逻辑测试页执行完成", True)
    for item in report.get("checks", []):
        check(f"浏览器: {item.get('name')}", bool(item.get("ok")), item.get("detail"))
    total = report.get("total") or 0
    passed = report.get("passed") or 0
    check(f"浏览器逻辑测试全部通过（{passed}/{total}）", total > 0 and passed == total)

    browser_records = [
        record for record in state.jobs
        if isinstance(record.get("json"), dict) and record["json"].get("title") == "测试视频标题"
    ]
    check("浏览器端真实 POST /api/jobs 到达 mock", len(browser_records) == 1, len(browser_records))
    if browser_records:
        sent = browser_records[-1]["json"]
        expect_equals("浏览器请求体: source_type", sent.get("source_type"), "tab")
        expect_equals("浏览器请求体: page_url", sent.get("page_url"), PAGE_URL)
        check("浏览器请求体: video_url 为 blob 地址", str(sent.get("video_url", "")).startswith("blob:"), sent.get("video_url"))
        expect_equals("浏览器请求体: duration 两位小数", sent.get("duration"), 754.23)
        expect_equals(
            "浏览器请求体: 字段集合符合契约 §8.3",
            set(sent),
            {"source_type", "page_url", "video_url", "title", "duration", "poster"},
        )
        check(
            "浏览器请求 Content-Type 为 application/json",
            "application/json" in (browser_records[-1]["headers"].get("Content-Type") or ""),
            browser_records[-1]["headers"].get("Content-Type"),
        )


# --------------------------------------------------------------------------- #
# popup 各状态 DOM 断言
# --------------------------------------------------------------------------- #

def run_preview_dom(mode: str) -> dict | None:
    page_url = f"{BASE}/dev/preview.html?mode={mode}"
    process = run_chrome(["--dump-dom", "--virtual-time-budget=12000", "--window-size=380,660", page_url])
    page_html = process.stdout if process is not None else ""
    report = extract_json_element(page_html, "report")
    if report is None:
        check(f"popup 预览[{mode}] 渲染完成", False, "未解析到 #report，页面可能 JS 异常")
        (DEV_DIR / f"debug-preview-{mode}.html").write_text(page_html or "", encoding="utf-8", errors="replace")
        return None
    check(f"popup 预览[{mode}] 渲染完成", True)
    snapshot = report.get("snapshot") or {}
    if snapshot.get("error"):
        check(f"popup 预览[{mode}] 无 JS 异常", False, snapshot.get("error"))
        return None
    check(f"popup 预览[{mode}] 无 JS 异常", True)
    return snapshot


def assert_popup_common(title: str, snapshot: dict) -> None:
    expect_equals(f"[{title}] popup 宽度 380px", snapshot.get("bodyWidth"), 380)
    height = snapshot.get("bodyHeight")
    check(f"[{title}] 高度不超过 Chrome popup 上限 600px", isinstance(height, int) and height <= 600, height)


def assert_popup_detected(snapshot: dict) -> None:
    title = "detected"
    expect_equals("[detected] 当前页面标题", snapshot.get("pageTitle"), PAGE_TITLE)
    expect_contains("[detected] 当前页面 URL", snapshot.get("pageUrl"), "bilibili.com")
    expect_equals("[detected] 检测结果标题", snapshot.get("detectTitle"), "检测到视频")
    expect_contains("[detected] 视频数量", snapshot.get("detectSub"), "2 个视频")
    expect_contains("[detected] 播放状态", snapshot.get("detectSub"), "正在播放")
    expect_equals("[detected] blob 徽标", snapshot.get("detectBadge"), "blob 流")
    expect_equals("[detected] 时长展示", snapshot.get("duration"), "12:34")
    expect_equals("[detected] 分辨率展示", snapshot.get("resolution"), "1920 × 1080")
    expect_equals("[detected] 来源展示", snapshot.get("source"), "内嵌流 (blob:)")
    expect_contains("[detected] blob 解析提示", snapshot.get("detectHint"), "blob")
    expect_equals("[detected] 后端状态", snapshot.get("backendText"), "本地服务已连接")
    expect_contains("[detected] 后端版本", snapshot.get("backendSub"), "v1.0.0")
    expect_contains("[detected] DeepSeek 状态", snapshot.get("backendSub"), "DeepSeek 已配置")
    expect_equals("[detected] 主按钮文案", snapshot.get("actionsLabel"), "分析这个视频")
    expect_equals("[detected] 主按钮可用", snapshot.get("analyzeDisabled"), False)
    expect_equals("[detected] 无错误提示", snapshot.get("errorHidden"), True)
    expect_equals("[detected] 成功卡片隐藏", snapshot.get("successHidden"), True)
    assert_popup_common(title, snapshot)


def assert_popup_offline(snapshot: dict) -> None:
    title = "offline"
    expect_equals("[offline] 后端状态", snapshot.get("backendText"), "本地服务未启动")
    expect_contains("[offline] 启动命令提示", snapshot.get("backendHint"), "run.ps1")
    expect_contains("[offline] 重试提示", snapshot.get("backendHint"), "重试")
    expect_equals("[offline] 主按钮禁用", snapshot.get("analyzeDisabled"), True)
    expect_equals("[offline] 网页入口禁用", snapshot.get("webDisabled"), True)
    expect_equals("[offline] 未弹出错误框", snapshot.get("errorHidden"), True)
    assert_popup_common(title, snapshot)


def assert_popup_novideo(snapshot: dict) -> None:
    title = "novideo"
    expect_equals("[novideo] 检测结果标题", snapshot.get("detectTitle"), "未检测到视频")
    expect_equals("[novideo] 主按钮文案", snapshot.get("actionsLabel"), "按页面链接分析")
    expect_equals("[novideo] 已知站点按钮可用", snapshot.get("analyzeDisabled"), False)
    expect_contains("[novideo] 已知站点提示", snapshot.get("detectHint"), "已知视频站点")
    expect_contains("[novideo] 无视频提示提到上传/链接", snapshot.get("detectHint"), "页面")
    assert_popup_common(title, snapshot)


def assert_popup_success(snapshot: dict) -> None:
    title = "success"
    expect_equals("[success] 成功卡片显示", snapshot.get("successHidden"), False)
    expect_equals("[success] 已提交标题", snapshot.get("successTitle"), PAGE_TITLE)
    expect_contains("[success] 作业信息", snapshot.get("successSub"), "job_demo01")
    expect_contains("[success] 作业状态", snapshot.get("successSub"), "排队中")
    expect_equals("[success] 主分析按钮隐藏", snapshot.get("analyzeHidden"), True)
    expect_equals("[success] 无错误提示", snapshot.get("errorHidden"), True)
    assert_popup_common(title, snapshot)


def popup_state_checks() -> None:
    section("4. Chrome headless：popup 各状态 DOM 断言（chrome API / fetch 打桩）")
    chrome = find_chrome()
    if chrome is None:
        check("找到 Chrome 可执行文件（popup 状态断言）", False)
        return

    detected = run_preview_dom("detected")
    if detected is not None:
        assert_popup_detected(detected)
    offline = run_preview_dom("offline")
    if offline is not None:
        assert_popup_offline(offline)
    novideo = run_preview_dom("novideo")
    if novideo is not None:
        assert_popup_novideo(novideo)
    success = run_preview_dom("success")
    if success is not None:
        assert_popup_success(success)


# --------------------------------------------------------------------------- #
# 截图
# --------------------------------------------------------------------------- #

def shoot(name: str, url: str, size: tuple[int, int]) -> None:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOTS_DIR / name
    if path.exists():
        path.unlink()
    run_chrome([
        "--virtual-time-budget=10000",
        f"--window-size={size[0]},{size[1]}",
        f"--screenshot={path}",
        url,
    ])
    exists = path.is_file() and path.stat().st_size > 2000
    check(f"截图 {name} 生成", exists, path)
    if not exists:
        return
    try:
        from PIL import Image
        with Image.open(path) as image:
            expect_equals(f"截图 {name} 尺寸", image.size, size)
    except Exception as exc:  # noqa: BLE001
        check(f"截图 {name} 可读取", False, exc)


def screenshot_checks() -> None:
    section("5. Chrome headless 截图（popup.html + popup 三种状态）")
    chrome = find_chrome()
    if chrome is None:
        check("找到 Chrome 可执行文件（截图）", False)
        return

    popup_uri = (EXT_DIR / "popup.html").as_uri()
    process = run_chrome(["--dump-dom", "--virtual-time-budget=8000", "--window-size=380,600", popup_uri])
    page_html = process.stdout if process is not None else ""
    check("popup.html 可直接在浏览器中渲染（无扩展环境降级预览）", 'data-app-ready="1"' in page_html and "预览模式" in page_html)

    shoot("popup-raw.png", popup_uri, (380, 600))
    shoot("preview-detected.png", f"{BASE}/dev/preview.html?mode=detected", (380, 660))
    shoot("preview-offline.png", f"{BASE}/dev/preview.html?mode=offline", (380, 660))
    shoot("preview-success.png", f"{BASE}/dev/preview.html?mode=success", (380, 660))


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main() -> int:
    print("视频分析 · DeepSeek —— Chrome 扩展自测", flush=True)
    print(f"扩展目录：{EXT_DIR}", flush=True)
    print(f"mock 后端：{BASE}（不占用 8765）", flush=True)

    static_checks()

    server = None
    state = None
    try:
        try:
            server, state = create_server(MOCK_PORT, str(EXT_DIR))
        except OSError as exc:
            check(f"mock 后端启动于 127.0.0.1:{MOCK_PORT}", False, exc)
            server = None
        if server is not None:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            time.sleep(0.3)
            check(f"mock 后端启动于 127.0.0.1:{MOCK_PORT}", True)
            mock_contract_checks(state)
            browser_logic_checks(state)
            popup_state_checks()
        screenshot_checks()
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()

    total = len(RESULTS)
    passed = sum(1 for item in RESULTS if item["ok"])
    failed = total - passed
    print("\n" + "=" * 64, flush=True)
    print(f"结果：{passed}/{total} 项通过" + (f"，{failed} 项失败" if failed else "，全部通过 ✅"), flush=True)
    if failed:
        print("失败项：", flush=True)
        for item in RESULTS:
            if not item["ok"]:
                print(f"  - {item['name']}  {item['detail'] or ''}", flush=True)

    report_path = DEV_DIR / "test-report.json"
    report_path.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "passed": passed,
                "total": total,
                "failed": failed,
                "results": RESULTS,
                "screenshots": [str(path) for path in sorted(SHOTS_DIR.glob("*.png"))],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"详细结果：{report_path}", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
