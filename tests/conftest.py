"""pytest 全局夹具：隔离数据目录、本地 DeepSeek stub、FastAPI 测试客户端。

关键点
------
1. **必须在导入 ``vanalyze`` 之前**把 ``VANALYZE_DATA_DIR`` 指向临时目录，
   否则测试会写入用户的 ``data/app.db`` / ``data/settings.json``（污染用户数据）。
2. 清掉环境中可能存在的真实 ``DEEPSEEK_API_KEY`` 等变量，保证「无 Key」用例稳定；
   真实 Key 验收由独立脚本 ``tests/real_key_check.py`` 完成（不经过本 conftest）。
3. ``api`` 夹具注入 ``JobManager`` 并替换后台 ``_run_job`` 为快速失败桩，
   API 测试不会触发真实 ASR / 网络下载；真实全链路在 ``tests/e2e.py`` 验证。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
# 保证无论从哪个目录运行 pytest，都能 import vanalyze / tests 内的辅助模块
for _path in (ROOT, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# ---------------------------------------------------------------------------
# 1) 环境隔离 —— 必须在任何 vanalyze 导入之前执行
# ---------------------------------------------------------------------------
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="vanalyze_tests_"))
os.environ["VANALYZE_DATA_DIR"] = str(_TEST_DATA_DIR)

#: 可能干扰测试的环境变量（真实用户环境 / 其他测试进程设置的覆盖项）
_SENSITIVE_ENV_VARS = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "VANALYZE_WHISPER_MODEL",
    "VANALYZE_WHISPER_DEVICE",
    "VANALYZE_WHISPER_COMPUTE_TYPE",
    "VANALYZE_WHISPER_LANGUAGE",
    "VANALYZE_MAX_FRAMES",
    "VANALYZE_FRAME_INTERVAL",
    "VANALYZE_VISION_BASE_URL",
    "VANALYZE_VISION_MODEL",
    "VANALYZE_VISION_API_KEY",
    "VANALYZE_PORT",
)
for _name in _SENSITIVE_ENV_VARS:
    os.environ.pop(_name, None)

#: 确保所有文本输出 UTF-8（Windows 控制台/管道）
os.environ.setdefault("PYTHONUTF8", "1")

from helpers import STUB_API_KEY, purge_runtime_data  # noqa: E402


# ---------------------------------------------------------------------------
# 2) Session 级：临时数据目录、stub 服务
# ---------------------------------------------------------------------------
def pytest_configure(config) -> None:
    """注册自定义 marker，避免 pytest 的 unknown marker 警告。"""
    config.addinivalue_line("markers", "e2e: 端到端测试（真实服务 + 真实 pipeline）")
    config.addinivalue_line("markers", "slow: 慢速测试（联网 / 大模型）")
    config.addinivalue_line("markers", "real_key: 需要真实 DeepSeek Key 的验收用例")
    config.addinivalue_line("markers", "extension: Chrome 扩展验收用例（静态检查 + headless 行为）")


@pytest.fixture(scope="session", autouse=True)
def _temp_data_dir_cleanup():
    """测试会话结束后清理临时数据目录（尽力而为，SQLite WAL 被占用时忽略）。"""
    yield
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def stub_deepseek():
    """本地 OpenAI 兼容 stub（进程内线程，端口随机）。"""
    from stub_deepseek import StubDeepSeek

    server = StubDeepSeek(port=0).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def _isolated_data(stub_deepseek):
    """每个测试前后：清空临时数据目录内容、重置 stub 行为与请求记录。"""
    purge_runtime_data()
    stub_deepseek.reset()
    try:
        yield
    finally:
        purge_runtime_data()
        stub_deepseek.reset()


# ---------------------------------------------------------------------------
# 3) FastAPI TestClient（注入独立 JobManager + 后台管线快速失败桩）
# ---------------------------------------------------------------------------
@pytest.fixture
def api(stub_deepseek):
    """返回 ``fastapi.testclient.TestClient``，已启动 lifespan。

    后台 ``JobManager._run_job`` 被替换为「立即失败」的桩：
    - 上传 / URL / Tab 创建作业的 API 行为可测；
    - 不会真的跑 Whisper、OCR、yt-dlp 下载（E2E 才跑真实管线）。
    """
    from fastapi.testclient import TestClient

    from vanalyze import store
    from vanalyze.server.app import create_app
    from vanalyze.server.jobs import JobManager

    manager = JobManager()

    def _fake_run_job(job_id, job_dir, source, settings):  # noqa: ANN001 - 测试桩
        try:
            store.update_job(job_id, {"status": "running"})
            manager._fail(job_id, "测试桩：API 测试不执行真实分析管线。")
        except Exception:  # noqa: BLE001 - 后台桩异常不应影响测试
            pass

    manager._run_job = _fake_run_job  # type: ignore[method-assign]

    app = create_app(manager)
    with TestClient(app) as client:
        yield client
    try:
        manager.shutdown(wait=False)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 4) Settings 工厂（core 单元测试用）
# ---------------------------------------------------------------------------
@pytest.fixture
def settings_factory(stub_deepseek):
    """构造指向 stub DeepSeek 的 ``Settings`` 实例。"""

    def _make(**overrides):
        from vanalyze.settings import Settings

        data = dict(
            deepseek_api_key=STUB_API_KEY,
            deepseek_base_url=stub_deepseek.base_url,
            deepseek_model="deepseek-chat",
            whisper_model="tiny",
            whisper_device="cpu",
            whisper_compute_type="int8",
            whisper_language="zh",
            max_frames=6,
            frame_interval=2.0,
        )
        data.update(overrides)
        return Settings(**data)

    return _make
