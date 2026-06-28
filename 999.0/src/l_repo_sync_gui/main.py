import argparse
import ast
import concurrent.futures
import ctypes
import datetime
import difflib
import faulthandler
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import webbrowser
import urllib.request

from git import Repo

from l_repo_sync_gui import git_helper, github_helper

try:
    import winreg  # 用于读取 Windows IE 代理
except ImportError:
    winreg = None
from pathlib import Path

from PySide6.QtCore import QEventLoop, QFile, QObject, QRunnable, Qt, QThread, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QTextBrowser,
    QTextEdit,
)
from l_qt_wgt_lib.smart_widget import CodeEditorWidget, LogCodeHighlighter
from l_qt_wgt_lib.tray_window import TrayAwareMixin
from pytracemp import lprint, LPrint

try:
    import shiboken6
except ImportError:
    shiboken6 = None


def _qt_alive(obj) -> bool:
    """判断 PySide6 包装的 C++ 对象是否仍存活（未被 deleteLater 删除）。"""
    if obj is None:
        return False
    if shiboken6 is None:
        return True
    try:
        return shiboken6.isValid(obj)
    except Exception:
        return False


IGNORE_LINES = [
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.exe",
    "*.dll",
    "*.so",
    "*.zip",
    "*.7z",
    "*.whl",
    "*.egg-info/",
    ".venv/",
    "py_312/",
]

# 按包名追加 .gitignore 规则（运行时数据、体积过大不宜 push GitHub）
PACKAGE_EXTRA_IGNORE: dict[str, list[str]] = {
    "l_log": [
        "999.0/backend/logs/",
        "999.0/backend/logs_cache/",
        "999.0/upload_test/",
    ],
}

GITHUB_FILE_WARN_BYTES = 50 * 1024 * 1024
GITHUB_FILE_LIMIT_BYTES = 100 * 1024 * 1024

SKIP_DIRS = {"repo_tools"}
PROTECTED_LOCAL_DELETE = {"l_repo_sync_gui"}
PREVIEW_MAX_LINES = 300
AI_MERGE_MAX_FILE_CHARS = 80000
# Git HTTPS：建立 TCP+TLS 连接阶段最长等待（秒），避免 SSL 握手长时间挂死
GIT_HTTP_CONNECT_TIMEOUT_SEC = 5
GIT_FETCH_TIMEOUT_SEC = 25
REMOTE_FETCH_MAX_WORKERS = 8
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"
# 国内硅基流动：直连，不走系统/环境代理（避免误走公司代理导致失败或变慢）
_SILICONFLOW_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}))


CHANGELOG_MARKDOWN = """# 更新日志

## 2026-06-20

### 修复 l_repo_sync_gui 启动后直接退出

- 修复 `RepoSyncWindow(TrayAwareMixin, QMainWindow)` 初始化后，调用 `setWindowTitle()` 时进程直接退出的问题。
- 根因是 `TrayAwareMixin` 继承了 `QWidget`，导致 PySide6 多继承时改变 Qt 对象初始化顺序，底层 `QMainWindow` 未按预期初始化。
- 将 `TrayAwareMixin` 改为纯 Python mixin，不再继承 `QWidget`，避免 Qt 原生层崩溃。
- 窗口标题不再拼接完整启动命令，改为固定显示 `Rez Package Repo Sync`，启动命令仅保存在内部字段中。

### 诊断改进

- 增加启动阶段日志，记录 `QApplication`、`RepoSyncWindow`、UI 加载、窗口显示等关键节点。
- 增加 `faulthandler` 崩溃日志，辅助排查 PySide6 / Qt 原生层直接退出的问题。
- 启动诊断日志路径：`%TEMP%/l_repo_sync_gui/logs/sync_YYYY-MM-DD.log`。
- 原生崩溃日志路径：`%TEMP%/l_repo_sync_gui/logs/fault_YYYY-MM-DD.log`。
"""


def _siliconflow_urlopen(req: urllib.request.Request, timeout: float):
    return _SILICONFLOW_OPENER.open(req, timeout=timeout)


# ---- AI 模型配置（多提供商）----
AI_CONFIG_FILE = Path.home() / ".l_repo_sync_gui_ai.json"
DEFAULT_SILICONFLOW_MODEL = "Qwen/Qwen2.5-72B-Instruct"
DEFAULT_SILICONFLOW_KEY = "sk-gzwtmzfhglvibdbvrttmsuuqsyyjxghxlxzdhubdefmshqoi"
DEFAULT_ZHIPU_KEY = "263c58d09135c4f088b0d436e3b89bfb.hXFGig2ucu4xe5PT"
# 内置提供商预设：provider -> (base_url, 默认模型, 默认模型列表)
AI_PROVIDER_PRESETS = {
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-72B-Instruct",
        "models": [
            "Qwen/Qwen2.5-72B-Instruct",
            "deepseek-ai/DeepSeek-V4-Flash",
            "Pro/zai-org/GLM-4.7",
        ],
        "default_key": DEFAULT_SILICONFLOW_KEY,
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "models": [
            "glm-4-flash",
            "glm-4-air",
            "glm-4-plus",
            "glm-4-long",
            "glm-4v-flash",
        ],
        "default_key": DEFAULT_ZHIPU_KEY,
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_key": "",
    },
    "custom": {
        "base_url": "",
        "model": "",
        "models": [],
        "default_key": "",
    },
}
MODEL_PRESETS = AI_PROVIDER_PRESETS["siliconflow"]["models"]  # 兼容老代码


def _load_ai_config() -> dict:
    """读取 AI 配置文件，返回配置字典。不存在则返回默认配置。"""
    default = {
        "enabled": True,
        "provider": "zhipu",
        "base_url": AI_PROVIDER_PRESETS["zhipu"]["base_url"],
        "model": AI_PROVIDER_PRESETS["zhipu"]["model"],
        "api_key": AI_PROVIDER_PRESETS["zhipu"]["default_key"],
        "model_presets": AI_PROVIDER_PRESETS["zhipu"]["models"],
    }
    if not AI_CONFIG_FILE.exists():
        return default
    try:
        data = json.loads(AI_CONFIG_FILE.read_text(encoding="utf-8"))
        # 填充缺失字段
        provider = data.get("provider", "zhipu")
        preset = AI_PROVIDER_PRESETS.get(provider, {})
        data.setdefault("enabled", True)
        data.setdefault("provider", provider)
        data.setdefault("base_url", preset.get("base_url", ""))
        data.setdefault("model", preset.get("model", ""))
        data.setdefault("api_key", preset.get("default_key", ""))
        data.setdefault("model_presets", preset.get("models", []))
        return data
    except Exception:
        return default


def _save_ai_config(data: dict):
    """保存 AI 配置到文件。"""
    AI_CONFIG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


AUTH_CONFIG_FILE = Path.home() / ".l_repo_sync_gui_auth.json"
NET_CONFIG_FILE = Path.home() / ".l_repo_sync_gui_net.json"
APP_ID = "lugwit.l_repo_sync_gui"
APP_ICON_FILE = Path(__file__).resolve().with_name("app_icon.svg")
RESOURCES_DIR = Path(__file__).resolve().parent / "resources"


def _find_rez_source_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if parent.name == "rez-package-source":
            return parent
    raise RuntimeError(
        "Cannot find rez-package-source from current script path.")


def _set_windows_app_id():
    """设置 Windows AppUserModelID，确保任务栏/通知图标归组一致。"""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        # 非关键能力，失败时静默回退。
        pass


def _runtime_log_path() -> Path:
    log_dir = Path(os.environ.get("TEMP", ".")) / "l_repo_sync_gui" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"sync_{datetime.datetime.now():%Y-%m-%d}.log"


def _write_runtime_log(message: str, *, echo: bool = False) -> None:
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    try:
        with _runtime_log_path().open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    if echo:
        try:
            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass


def parse_rez_package_aliases(package_text: str) -> list[tuple[str, str]]:
    """从 Rez package.py 文本中解析 alias(name, command)。

    返回 [(alias_name, alias_command), ...]，只接受静态字符串字面量，避免执行 package.py。
    """
    aliases: list[tuple[str, str]] = []
    try:
        tree = ast.parse(package_text)
    except SyntaxError:
        return aliases
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_alias = isinstance(func, ast.Name) and func.id == "alias"
        if not is_alias or len(node.args) < 2:
            continue
        try:
            alias_name = ast.literal_eval(node.args[0])
            alias_command = ast.literal_eval(node.args[1])
        except Exception:
            continue
        if isinstance(alias_name, str) and isinstance(alias_command, str):
            aliases.append((alias_name, alias_command))
    return aliases


def format_rez_alias_launch_cmd(
    pkg_name: str,
    alias_name: str,
    *,
    wuwo_bat: Path | None = None,
) -> str:
    if wuwo_bat is not None:
        return f"wuwor {pkg_name} .ps .solo -- {alias_name}"
    return f"wuwo {pkg_name} .ps .solo -- {alias_name}"


class _AiBridge(QObject):
    finished = Signal(bool, str)


class _NetTestBridge(QObject):
    finished = Signal(bool, str)


class _PackageRefreshBridge(QObject):
    list_ready = Signal(int, list, object, list, bool)
    status_ready = Signal(int, object)
    failed = Signal(int, str)
    one_ready = Signal(str, bool, object, object, str)
    log = Signal(str)


class _AiStreamBridge(QObject):
    status = Signal(str)
    chunk = Signal(str)
    finished = Signal(bool, str)


class _AiMergePreviewBridge(QObject):
    finished = Signal(str, bool, str)


class _FnRunnable(QRunnable):
    """在 Qt 托管的线程池中运行任意无参函数。

    使用 QThreadPool 而非裸 threading.Thread 是为了让后台线程由 Qt 管理：
    池线程是持久的 QThread，携带正确的 QThreadData，结束时不会触发
    "QThreadStorage: entry destroyed before end of thread" → abort() 崩溃。
    在池线程中 emit 跨线程信号是 Qt 官方支持的安全模式。
    """

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        self._fn()


class _MainThreadInvoker(QObject):
    """把任意无参函数调度回主线程执行。

    在主线程创建本对象；从后台（池）线程调用 post(fn) 时，信号以
    QueuedConnection 投递到主线程事件循环，由主线程执行 fn，从而安全地
    更新控件 / 弹窗，避免跨线程访问 Qt 对象导致 abort()。
    """

    _sig = Signal(object)

    def __init__(self):
        super().__init__()
        self._sig.connect(self._invoke, Qt.QueuedConnection)

    @staticmethod
    def _invoke(fn):
        try:
            fn()
        except Exception:
            pass

    def post(self, fn):
        self._sig.emit(fn)


class _DiffHighlighter(QSyntaxHighlighter):
    """简单 diff 语法高亮。"""

    def __init__(self, parent):
        super().__init__(parent)
        self._fmt_add = QTextCharFormat()
        self._fmt_add.setForeground(QColor("#7CFC00"))
        self._fmt_del = QTextCharFormat()
        self._fmt_del.setForeground(QColor("#FF6B6B"))
        self._fmt_hunk = QTextCharFormat()
        self._fmt_hunk.setForeground(QColor("#7AA2F7"))
        self._fmt_file = QTextCharFormat()
        self._fmt_file.setForeground(QColor("#E5C07B"))

    def highlightBlock(self, text: str):
        if text.startswith("+++ ") or text.startswith("--- ") or text.startswith("diff --git "):
            self.setFormat(0, len(text), self._fmt_file)
        elif text.startswith("@@"):
            self.setFormat(0, len(text), self._fmt_hunk)
        elif text.startswith("+"):
            self.setFormat(0, len(text), self._fmt_add)
        elif text.startswith("-"):
            self.setFormat(0, len(text), self._fmt_del)


class RepoSyncWindow(TrayAwareMixin, QMainWindow):
    def __init__(
        self,
        launch_mode: str | None = None,
        *,
        refresh_status_on_start: bool = False,
        launch_command: str | None = None,
    ):
        super().__init__()
        self.rez_source = _find_rez_source_root()
        self._refresh_status_on_start = refresh_status_on_start
        title_parts = ["Rez Package Repo Sync"]
        if launch_mode:
            title_parts.append(launch_mode)
        self._launch_command = launch_command or ""
        self.setWindowTitle(" - ".join(title_parts))
        self.resize(1280, 820)
        self.setMinimumSize(1100, 700)
        # 防重入标志：防止 _run_blocking_with_events 嵌套调用
        self._blocking_in_progress = False
        # 后台任务统一走 Qt 托管线程池（QThreadPool），避免裸 threading.Thread
        # 触碰 Qt（emit 信号 / 访问控件）后线程销毁触发 QThreadStorage abort。
        # 放宽线程上限，避免 _run_blocking_with_events 的阻塞等待与长任务
        # （如 AI 流式请求）抢占线程导致排队死等。
        QThreadPool.globalInstance().setMaxThreadCount(32)
        # 主线程调度器：worker 线程通过它把 UI 更新 marshal 回主线程。
        self._main_invoker = _MainThreadInvoker()
        if APP_ICON_FILE.exists():
            self.setWindowIcon(QIcon(str(APP_ICON_FILE)))

        self.row_buttons = []
        self.package_row_buttons: dict[str, dict[str, QPushButton]] = {}
        self.package_entries: list[tuple[str, Path, bool]] = []
        self.package_status_labels: dict[str, QLabel] = {}
        self.package_sync_map: dict[str, dict[str, int]] = {}
        self.package_merge_buttons: dict[str, QPushButton] = {}
        self.package_refresh_buttons: dict[str, QPushButton] = {}
        # 每行勾选框：供「上传选中」批量上传使用（按包名跟踪，跨行复用保留）
        self.package_checkboxes: dict[str, QCheckBox] = {}
        self.btn_upload_selected: QPushButton | None = None
        self.btn_refresh_status: QPushButton | None = None
        self.btn_refresh_packages: QPushButton | None = None
        self.ai_ask_btn: QPushButton | None = None
        self.debug_start_pkg: QCheckBox | None = None
        self._package_refresh_token = 0
        self._package_refresh_running = False
        self._last_list_scan_status = True
        self._package_alias_cache: dict[str, list[str]] = {}
        self._package_refresh_bridge = _PackageRefreshBridge()
        self._package_refresh_bridge.list_ready.connect(
            self._on_package_list_ready)
        self._package_refresh_bridge.status_ready.connect(
            self._on_package_status_ready)
        self._package_refresh_bridge.failed.connect(
            self._on_package_refresh_failed)
        self._package_refresh_bridge.one_ready.connect(
            self._on_one_package_status_ready)
        self._package_refresh_bridge.log.connect(self._log)
        self.ai_bridge = _AiBridge()
        self.ai_bridge.finished.connect(self._on_ai_result)

        self._load_style()
        self._load_ui()
        self._log_file_handler = None
        self._init_log_file()
        QTimer.singleShot(
            0,
            lambda: self._start_package_list_refresh(
                scan_status=self._refresh_status_on_start),
        )

    def _load_style(self):
        qss_path = RESOURCES_DIR / "style.qss"
        if qss_path.exists():
            self.setStyleSheet(qss_path.read_text(encoding="utf-8"))

    def closeEvent(self, event):
        """窗口关闭时清理资源。"""
        if hasattr(self, "_log_file_handler") and self._log_file_handler:
            try:
                self._log_file_handler.close()
            except Exception:
                pass
        super().closeEvent(event)

    def on_tray_message_log(self, message: str) -> None:
        self._log(message)

    def _load_ui(self):
        ui_path = RESOURCES_DIR / "main_window.ui"
        loader = QUiLoader()
        ui_file = QFile(str(ui_path))
        ui_file.open(QFile.ReadOnly)
        central = loader.load(ui_file, self)
        ui_file.close()
        if central is None:
            raise RuntimeError(
                f"QUiLoader failed to load UI: {ui_path}; error={loader.errorString()}")
        self.setCentralWidget(central)

        # 绑定 .ui 中通过 objectName 定义的控件
        self.owner_edit = central.findChild(QLineEdit, "owner_edit")
        self.git_path_edit = central.findChild(QLineEdit, "git_path_edit")
        self.gh_path_edit = central.findChild(QLineEdit, "gh_path_edit")
        lbl_gh = central.findChild(QLabel, "lbl_gh")
        if lbl_gh is not None:
            lbl_gh.setVisible(False)
        if self.gh_path_edit is not None:
            self.gh_path_edit.setVisible(False)
        lbl_git = central.findChild(QLabel, "lbl_git")
        if lbl_git is not None:
            lbl_git.setVisible(False)
        if self.git_path_edit is not None:
            self.git_path_edit.setVisible(False)
        self.gh_token_edit = central.findChild(QLineEdit, "gh_token_edit")
        self.btn_model_settings = central.findChild(
            QPushButton, "btn_model_settings")
        self.model_edit = central.findChild(QComboBox, "model_edit")
        self.ai_prompt_edit = central.findChild(QTextEdit, "ai_prompt_edit")
        self.ai_answer_edit = central.findChild(QTextEdit, "ai_answer_edit")
        log_placeholder = central.findChild(QTextEdit, "log_edit")
        self.list_area = central.findChild(QScrollArea, "list_area")
        self.debug_start_pkg = central.findChild(QCheckBox, "debug_start_pkg")
        self.run_buttonGroup = central.findChild(
            QButtonGroup, "run_buttonGroup")
        self.solo_checkBox = central.findChild(QCheckBox, "solo_checkBox")

        # ---- 日志组件替换为 CodeEditorWidget（带行号 + 日志语法高亮）----
        parent_container = log_placeholder.parentWidget()
        geo = log_placeholder.geometry()
        size_pol = log_placeholder.sizePolicy()
        min_h = log_placeholder.minimumHeight()
        max_h = log_placeholder.maximumHeight()
        stretch = 0

        parent_layout = parent_container.layout() if parent_container else None
        if parent_layout is not None:
            idx = parent_layout.indexOf(log_placeholder)
            if idx >= 0:
                # getItemPosition 仅 QGridLayout 支持；QBoxLayout 用 stretch()
                if hasattr(parent_layout, "getItemPosition"):
                    _, _, _, stretch = parent_layout.getItemPosition(idx)
                else:
                    stretch = parent_layout.stretch(idx)
                parent_layout.removeItem(parent_layout.itemAt(idx))
        log_placeholder.setParent(None)
        log_placeholder.deleteLater()

        self.log_edit = CodeEditorWidget(parent_container)
        self.log_edit.setObjectName("log_edit")
        self.log_edit.setReadOnly(True)
        self.log_edit.set_highlighter(LogCodeHighlighter)
        self.log_edit.setPlaceholderText("日志输出（右键可切换高亮模式，Ctrl+滚轮缩放字体）")
        self.log_edit.setGeometry(geo)
        self.log_edit.setSizePolicy(size_pol)
        if min_h > 0:
            self.log_edit.setMinimumHeight(min_h)
        if max_h < 16777215:
            self.log_edit.setMaximumHeight(max_h)

        if parent_layout is not None and idx >= 0:
            parent_layout.insertWidget(idx, self.log_edit, stretch=stretch)
        self.list_widget = central.findChild(QWidget, "list_widget")
        self.list_layout = self.list_widget.layout() if self.list_widget else None
        self.btn_refresh_packages = central.findChild(
            QPushButton, "btn_refresh_packages")
        self.btn_refresh_status = central.findChild(
            QPushButton, "btn_refresh_status")
        self.ai_ask_btn = central.findChild(QPushButton, "ai_ask_btn")
        self.btn_upload_selected = central.findChild(
            QPushButton, "btn_upload_selected")
        btn_upload_all = central.findChild(QPushButton, "btn_upload_all")
        btn_download_all = central.findChild(QPushButton, "btn_download_all")
        btn_restart = central.findChild(QPushButton, "btn_restart")
        update_log_btn = central.findChild(QPushButton, "update_log_btn")
        btn_save_token = central.findChild(QPushButton, "btn_save_token")
        btn_clear_token = central.findChild(QPushButton, "btn_clear_token")
        btn_auth_status = central.findChild(QPushButton, "btn_auth_status")
        self.btn_test_network = central.findChild(
            QPushButton, "btn_test_network")
        btn_network_settings = central.findChild(
            QPushButton, "btn_network_settings")
        btn_gh_login = central.findChild(QPushButton, "btn_gh_login")
        btn_model_test = central.findChild(QPushButton, "btn_model_test")
        body_splitter = central.findChild(QSplitter, "body_splitter")
        left_panel = central.findChild(QSplitter, "left_panel")

        # list_layout 对齐方式（.ui 中无法直接设 AlignTop）
        if self.list_layout:
            self.list_layout.setAlignment(Qt.AlignTop)

        # 初始化动态属性
        # 加载 AI 配置（provider / api_key / model / presets）
        self._ai_config = _load_ai_config()
        self._refresh_model_edit_from_config()
        if self.btn_model_settings:
            self.btn_model_settings.clicked.connect(
                self._open_model_settings_dialog)
        self._log(
            f"[info] AI 配置: provider={self._ai_config.get('provider')} "
            f"model={self._ai_config.get('model')} "
            f"enabled={self._ai_config.get('enabled')}"
        )

        # 分割器尺寸（.ui 不可靠，代码保证）
        if body_splitter:
            body_splitter.setStretchFactor(0, 3)
            body_splitter.setStretchFactor(1, 2)
            body_splitter.setSizes([760, 500])
        if left_panel:
            left_panel.setStretchFactor(0, 3)
            left_panel.setStretchFactor(1, 2)
            left_panel.setSizes([430, 260])

        # 信号连接
        self.btn_refresh_packages.clicked.connect(self.refresh_packages)
        self.btn_refresh_status.clicked.connect(self.refresh_package_status)
        btn_upload_all.clicked.connect(self.upload_all)
        if self.btn_upload_selected is not None:
            self.btn_upload_selected.clicked.connect(self.upload_selected)
        btn_download_all.clicked.connect(self.download_all)
        btn_restart.clicked.connect(self.restart_self)
        if update_log_btn:
            update_log_btn.clicked.connect(self._open_update_log_dialog)
        btn_save_token.clicked.connect(self._save_auth_settings)
        btn_clear_token.clicked.connect(self._clear_auth_token)
        btn_auth_status.clicked.connect(self._check_github_auth)
        self.btn_test_network.clicked.connect(self._test_network_connection)
        btn_network_settings.clicked.connect(
            self._open_network_settings_dialog)
        btn_gh_login.clicked.connect(self._start_gh_auth_login)
        btn_model_test.clicked.connect(self._test_model_connection)
        self.ai_ask_btn.clicked.connect(self.ask_ai)

        # 网络测试桥接
        self._net_test_bridge = _NetTestBridge()
        self._net_test_bridge.finished.connect(self._on_network_test_finished)
        self._net_test_running = False

        self._load_auth_settings()
        self._load_network_settings()

    def _open_update_log_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("更新日志")
        dlg.resize(780, 620)

        layout = QVBoxLayout(dlg)
        browser = QTextBrowser(dlg)
        browser.setOpenExternalLinks(True)
        browser.setMarkdown(CHANGELOG_MARKDOWN)
        layout.addWidget(browser)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, dlg)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()

    def _github_token(self) -> str:
        return (self.gh_token_edit.text() or "").strip()

    def _load_auth_settings(self):
        """加载本地 GitHub token 配置。"""
        try:
            if not AUTH_CONFIG_FILE.exists():
                return
            data = json.loads(AUTH_CONFIG_FILE.read_text(encoding="utf-8"))
            token = str(data.get("github_token") or "").strip()
            if token:
                self.gh_token_edit.setText(token)
        except Exception as exc:
            self._log(f"[WARN] 加载授权配置失败: {exc}")

    def _save_auth_settings(self):
        """保存 GitHub token 到本地配置文件。"""
        token = (self.gh_token_edit.text() or "").strip()
        payload = {"github_token": token}
        try:
            AUTH_CONFIG_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._log(f"[ok] 已保存授权配置: {AUTH_CONFIG_FILE}")
            QMessageBox.information(self, "保存成功", "GitHub Token 已保存。")
        except Exception as exc:
            self._log(f"[ERR] 保存授权配置失败: {exc}")
            QMessageBox.warning(self, "保存失败", str(exc))

    def _clear_auth_token(self):
        self.gh_token_edit.clear()
        self._save_auth_settings()
        self._log("[info] 已清除 GitHub Token。")

    # ------------------------------------------------------------------
    # 网络设置（代理 / NO_PROXY / git 代理 / HTTP 版本）
    # ------------------------------------------------------------------
    HTTP_VERSION_CHOICES = [
        ("自动（默认）", ""),
        ("HTTP/1.1（降低 SSL_ERROR_SYSCALL 概率）", "HTTP/1.1"),
        ("HTTP/2（更快但某些代理不兼容）", "HTTP/2"),
    ]

    PROXY_MODE_CHOICES = [
        ("不使用代理", "none"),
        ("使用 IE 代理（读取 Windows Internet 设置）", "ie"),
        ("自定义代理", "custom"),
    ]

    # ------------------------------------------------------------------
    # 读取 Windows IE 代理
    # ------------------------------------------------------------------
    def _read_ie_proxy(self) -> dict:
        """读取 Windows IE 代理设置，返回 {'enable': bool, 'server': str, 'override': str}。"""
        result = {"enable": False, "server": "", "override": ""}
        if winreg is None:
            return result
        try:
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                try:
                    enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                    result["enable"] = bool(enable)
                except FileNotFoundError:
                    pass
                try:
                    server, _ = winreg.QueryValueEx(key, "ProxyServer")
                    result["server"] = str(server or "")
                except FileNotFoundError:
                    pass
                try:
                    override, _ = winreg.QueryValueEx(key, "ProxyOverride")
                    result["override"] = str(override or "")
                except FileNotFoundError:
                    pass
        except Exception:
            pass
        return result

    def _parse_ie_proxy_server(self, server: str) -> dict:
        """将 IE ProxyServer 字符串解析为 {http, https, socks}。
        支持格式：
        - 单一地址：'host:port'
        - 分协议：'http=host:port;https=host:port;socks=host:port'
        - FTP 前缀忽略：'ftp=...;http=...;https=...'
        """
        parsed = {"http": "", "https": "", "socks": ""}
        if not server:
            return parsed
        # 如果不含 '='，视为单一地址同时应用于 http/https
        if "=" not in server:
            parsed["http"] = f"http://{server}"
            parsed["https"] = f"http://{server}"
            return parsed
        for part in server.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k = k.strip().lower()
            v = v.strip()
            if not v:
                continue
            # 自动加 http:// 前缀（如果没写协议）
            if "://" not in v:
                v = f"http://{v}"
            if k == "http":
                parsed["http"] = v
            elif k == "https":
                parsed["https"] = v
            elif k in ("socks", "socks5", "socks4"):
                # IE 的 socks 字段一般不带 socks:// 前缀
                parsed["socks"] = v
        return parsed

    def _ie_override_to_no_proxy(self, override: str) -> str:
        """将 IE ProxyOverride（如 '<local>;*.company.com'）转为 NO_PROXY 格式。"""
        if not override:
            return ""
        items = [x.strip() for x in override.split(";") if x.strip()]
        # 过滤掉 <local>（表示本地地址）并替换为常见本地域名
        out = []
        for it in items:
            if it.lower() == "<local>":
                out.extend(["localhost", "127.0.0.1"])
            else:
                out.append(it)
        # 去重保持顺序
        seen = set()
        result = []
        for x in out:
            if x not in seen:
                seen.add(x)
                result.append(x)
        return ",".join(result)

    def _apply_git_http_version(self, version: str):
        """应用 git http.version 全局配置。version 为空字符串时清除配置。"""
        try:
            git_helper.apply_global_http_version(version)
        except Exception:
            pass

    def _load_network_settings(self):
        """从本地配置文件读取网络设置，并应用到环境变量（仅本进程）。"""
        try:
            if not NET_CONFIG_FILE.exists():
                return
            data = json.loads(NET_CONFIG_FILE.read_text(encoding="utf-8"))
            if not data.get("enabled", False):
                self._log("[info] 网络配置存在但未启用。")
                return

            proxy_mode = (data.get("proxy_mode") or "none").strip()
            http_version = (data.get("http_version") or "").strip()
            apply_git = bool(data.get("apply_git_proxy", False))
            git_proxy = (data.get("git_proxy") or "").strip()

            # 根据 proxy_mode 决定实际使用的代理地址
            http_proxy = https_proxy = all_proxy = no_proxy = ""
            mode_label = "不使用代理"

            if proxy_mode == "ie":
                ie = self._read_ie_proxy()
                if ie["enable"] and ie["server"]:
                    parsed = self._parse_ie_proxy_server(ie["server"])
                    http_proxy = parsed["http"]
                    https_proxy = parsed["https"]
                    all_proxy = parsed["socks"]
                    no_proxy = self._ie_override_to_no_proxy(ie["override"])
                    mode_label = f"IE 代理 ({ie['server']})"
                else:
                    mode_label = "IE 代理（未启用）"
            elif proxy_mode == "custom":
                http_proxy = (data.get("http_proxy") or "").strip()
                https_proxy = (data.get("https_proxy") or "").strip()
                all_proxy = (data.get("all_proxy") or "").strip()
                no_proxy = (data.get("no_proxy") or "").strip()
                mode_label = "自定义代理"
            else:
                mode_label = "不使用代理"

            # 清除已有的代理环境变量（避免残留）
            for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                        "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
                os.environ.pop(key, None)

            # 应用新的代理环境变量（仅当不是 "none" 模式）
            if proxy_mode != "none":
                # 自动追加 github.com / api.github.com 到 NO_PROXY
                # 原因：某些代理（如 Clash）对 api.github.com 的 TLS 支持异常，
                #       而 github.com 通常直连即可，避免 gh CLI 误走代理导致超时。
                extra_no_proxy = ["github.com", "api.github.com"]
                if no_proxy:
                    existing = [x.strip()
                                for x in no_proxy.split(",") if x.strip()]
                    for d in extra_no_proxy:
                        if d not in existing and not any(
                            p.startswith(".") and d.endswith(p[1:]) for p in existing if p.startswith(".")
                        ):
                            existing.append(d)
                    no_proxy = ",".join(existing)
                else:
                    no_proxy = ",".join(extra_no_proxy)

                if http_proxy:
                    os.environ["HTTP_PROXY"] = http_proxy
                    os.environ["http_proxy"] = http_proxy
                if https_proxy:
                    os.environ["HTTPS_PROXY"] = https_proxy
                    os.environ["https_proxy"] = https_proxy
                if all_proxy:
                    os.environ["ALL_PROXY"] = all_proxy
                    os.environ["all_proxy"] = all_proxy
                if no_proxy:
                    os.environ["NO_PROXY"] = no_proxy
                    os.environ["no_proxy"] = no_proxy

            # 根据模式决定 git 全局代理（GitPython 写 ~/.gitconfig）
            effective_git_proxy = ""
            if proxy_mode == "ie" and (http_proxy or https_proxy):
                effective_git_proxy = https_proxy or http_proxy
            elif proxy_mode == "custom" and apply_git and git_proxy:
                effective_git_proxy = git_proxy

            if effective_git_proxy:
                git_helper.apply_global_proxy(
                    effective_git_proxy, effective_git_proxy)
            else:
                git_helper.apply_global_proxy(None, None)

            self._apply_git_http_version(http_version)

            self._log(
                f"[ok] 已应用网络配置: "
                f"模式={mode_label} "
                f"HTTP={http_proxy or '-'} "
                f"HTTPS={https_proxy or '-'} "
                f"NO_PROXY={no_proxy or '-'} "
                f"http.version={http_version or 'default'}"
            )
        except Exception as exc:
            self._log(f"[WARN] 加载网络配置失败: {exc}")

    def _save_network_settings_to_file(self, payload: dict):
        """保存网络配置到 JSON 文件。"""
        NET_CONFIG_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_network_settings_from_file(self) -> dict:
        try:
            if not NET_CONFIG_FILE.exists():
                return {}
            return json.loads(NET_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _open_network_settings_dialog(self):
        """弹出网络设置对话框。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("网络设置")
        dlg.resize(600, 540)

        root_layout = QVBoxLayout(dlg)

        # ---- 全局启用 ----
        chk_enabled = QCheckBox("启用网络设置（关闭后不应用任何代理）")

        # ---- 代理模式分组 ----
        grp_mode = QGroupBox("代理模式")
        mode_layout = QVBoxLayout(grp_mode)
        cb_mode = QComboBox()
        for label, _ in self.PROXY_MODE_CHOICES:
            cb_mode.addItem(label)
        mode_layout.addWidget(cb_mode)

        # IE 代理状态信息
        ie_info_label = QLabel()
        ie_info_label.setWordWrap(True)
        ie_info_label.setStyleSheet("color: #555; font-size: 11px;")
        ie_info_label.setVisible(False)
        mode_layout.addWidget(ie_info_label)

        # ---- 自定义代理分组 ----
        grp_proxy = QGroupBox("自定义代理（仅当模式为「自定义代理」时生效）")
        form_proxy = QFormLayout(grp_proxy)
        ed_http = QLineEdit()
        ed_http.setPlaceholderText(
            "http://user:pass@host:port  或 socks5://host:port")
        ed_https = QLineEdit()
        ed_https.setPlaceholderText(
            "http://user:pass@host:port  或 socks5://host:port")
        ed_all = QLineEdit()
        ed_all.setPlaceholderText("socks5://host:port  （同时覆盖 HTTP/HTTPS）")
        ed_no = QLineEdit()
        ed_no.setPlaceholderText("localhost,127.0.0.1,.company.com")
        form_proxy.addRow("HTTP_PROXY:", ed_http)
        form_proxy.addRow("HTTPS_PROXY:", ed_https)
        form_proxy.addRow("ALL_PROXY:", ed_all)
        form_proxy.addRow("NO_PROXY:", ed_no)

        # ---- git 代理分组 ----
        grp_git = QGroupBox("Git 设置（写入 git config --global，重启后仍生效）")
        form_git = QFormLayout(grp_git)
        chk_git = QCheckBox("启用 git 代理（仅「自定义」模式需勾选；「IE」模式自动写入）")
        ed_git = QLineEdit()
        ed_git.setPlaceholderText("http://host:port  或 socks5://host:port")
        cb_http_version = QComboBox()
        for label, _ in self.HTTP_VERSION_CHOICES:
            cb_http_version.addItem(label)
        form_git.addRow(chk_git)
        form_git.addRow("git http/https.proxy:", ed_git)
        form_git.addRow("git http.version:", cb_http_version)

        # ---- 按钮 ----
        btn_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Reset
        )
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        btn_reset = btn_box.button(QDialogButtonBox.Reset)

        # ---- 模式切换回调 ----
        def _on_mode_changed(idx: int):
            mode = self.PROXY_MODE_CHOICES[idx][1]
            custom_enabled = (mode == "custom")
            ed_http.setEnabled(custom_enabled)
            ed_https.setEnabled(custom_enabled)
            ed_all.setEnabled(custom_enabled)
            ed_no.setEnabled(custom_enabled)
            ed_git.setEnabled(custom_enabled)
            chk_git.setEnabled(custom_enabled)

            if mode == "ie":
                ie = self._read_ie_proxy()
                if ie["enable"] and ie["server"]:
                    parsed = self._parse_ie_proxy_server(ie["server"])
                    info_lines = [
                        f"检测到 IE 代理: {ie['server']}",
                        f"HTTP={parsed['http'] or '-'}  HTTPS={parsed['https'] or '-'}  SOCKS={parsed['socks'] or '-'}",
                    ]
                    if ie["override"]:
                        info_lines.append(f"ProxyOverride: {ie['override']}")
                    ie_info_label.setText("\n".join(info_lines))
                else:
                    ie_info_label.setText("IE 代理未启用或无 ProxyServer 配置。")
                ie_info_label.setVisible(True)
            else:
                ie_info_label.setVisible(False)

        cb_mode.currentIndexChanged.connect(_on_mode_changed)

        # ---- 加载现有配置 ----
        cur = self._read_network_settings_from_file()
        # 加载模式
        cur_mode = (cur.get("proxy_mode") or "none").strip()
        cb_mode.setCurrentIndex(0)
        for i, (_, v) in enumerate(self.PROXY_MODE_CHOICES):
            if v == cur_mode:
                cb_mode.setCurrentIndex(i)
                break
        ed_http.setText(cur.get("http_proxy", ""))
        ed_https.setText(cur.get("https_proxy", ""))
        ed_all.setText(cur.get("all_proxy", ""))
        ed_no.setText(cur.get("no_proxy", ""))
        chk_git.setChecked(bool(cur.get("apply_git_proxy", False)))
        ed_git.setText(cur.get("git_proxy", ""))
        cur_ver = (cur.get("http_version") or "").strip()
        cb_http_version.setCurrentIndex(0)
        for i, (_, v) in enumerate(self.HTTP_VERSION_CHOICES):
            if v == cur_ver:
                cb_http_version.setCurrentIndex(i)
                break
        chk_enabled.setChecked(bool(cur.get("enabled", False)))
        _on_mode_changed(cb_mode.currentIndex())

        def _on_reset():
            cb_mode.setCurrentIndex(0)  # "不使用代理"
            ed_http.clear()
            ed_https.clear()
            ed_all.clear()
            ed_no.clear()
            ed_git.clear()
            chk_git.setChecked(False)
            cb_http_version.setCurrentIndex(0)
            chk_enabled.setChecked(False)
            _on_mode_changed(0)

        btn_reset.clicked.connect(_on_reset)

        root_layout.addWidget(chk_enabled)
        root_layout.addWidget(grp_mode)
        root_layout.addWidget(grp_proxy)
        root_layout.addWidget(grp_git)
        root_layout.addWidget(btn_box)

        result = dlg.exec()
        if result != QDialog.Accepted:
            self._log("[info] 网络设置已取消。")
            return

        payload = {
            "enabled": chk_enabled.isChecked(),
            "proxy_mode": self.PROXY_MODE_CHOICES[cb_mode.currentIndex()][1],
            "http_proxy": ed_http.text().strip(),
            "https_proxy": ed_https.text().strip(),
            "all_proxy": ed_all.text().strip(),
            "no_proxy": ed_no.text().strip(),
            "apply_git_proxy": chk_git.isChecked(),
            "git_proxy": ed_git.text().strip(),
            "http_version": self.HTTP_VERSION_CHOICES[cb_http_version.currentIndex()][1],
        }
        try:
            self._save_network_settings_to_file(payload)
            self._log(f"[ok] 网络配置已保存: {NET_CONFIG_FILE}")
            # 重新加载并应用到当前进程
            self._load_network_settings()
            QMessageBox.information(
                self,
                "保存成功",
                f"网络配置已保存到:\n{NET_CONFIG_FILE}\n\n"
                "（代理环境变量已在当前进程生效；git 代理已写入全局配置）",
            )
        except Exception as exc:
            self._log(f"[ERR] 保存网络配置失败: {exc}")
            QMessageBox.warning(self, "保存失败", str(exc))

    def _open_model_settings_dialog(self):
        """弹出 AI 模型设置对话框。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("AI 模型设置")
        dlg.resize(620, 560)
        root = QVBoxLayout(dlg)

        cfg = dict(self._ai_config or {})

        # ---- 全局启用 ----
        chk_enabled = QCheckBox("启用 AI 模型功能（取消勾选后所有 AI 调用将被跳过）")
        chk_enabled.setChecked(bool(cfg.get("enabled", True)))
        root.addWidget(chk_enabled)

        # ---- 提供商 ----
        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("提供商:"))
        cmb_provider = QComboBox()
        provider_labels = [
            ("zhipu", "智谱 AI (Zhipu BigModel)"),
            ("siliconflow", "硅基流动 (SiliconFlow)"),
            ("deepseek", "DeepSeek"),
            ("custom", "自定义 (OpenAI 兼容)"),
        ]
        for key, label in provider_labels:
            cmb_provider.addItem(label, key)
        current_provider = cfg.get("provider", "zhipu")
        for i in range(cmb_provider.count()):
            if cmb_provider.itemData(i) == current_provider:
                cmb_provider.setCurrentIndex(i)
                break
        provider_row.addWidget(cmb_provider, 1)
        root.addLayout(provider_row)

        # ---- Base URL ----
        root.addWidget(QLabel("Base URL (OpenAI 兼容 API 根地址):"))
        edit_base = QLineEdit()
        edit_base.setPlaceholderText(
            "例如: https://open.bigmodel.cn/api/paas/v4")
        edit_base.setText(cfg.get("base_url", ""))
        root.addWidget(edit_base)

        # ---- API Key ----
        root.addWidget(QLabel("API Key:"))
        edit_key = QLineEdit()
        edit_key.setEchoMode(QLineEdit.Password)
        edit_key.setPlaceholderText("粘贴你的 API Key")
        edit_key.setText(cfg.get("api_key", ""))
        root.addWidget(edit_key)

        # ---- 当前模型 ----
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("当前模型:"))
        cmb_model = QComboBox()
        # 保持可编辑：既能下拉选，也能手动输入任意模型名
        cmb_model.setEditable(True)
        cmb_model.setInsertPolicy(QComboBox.NoInsert)
        # 强调下拉箭头视觉效果
        cmb_model.setStyleSheet(
            "QComboBox { padding: 2px 6px; }"
            "QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; "
            "                    width: 24px; border-left: 1px solid #555; }"
            "QComboBox::down-arrow { image: none; border-left: 5px solid transparent; "
            "                       border-right: 5px solid transparent; border-top: 7px solid palette(text); "
            "                       margin-right: 6px; }"
        )
        presets = list(cfg.get("model_presets") or [])
        cur_model = (cfg.get("model") or "").strip()
        if cur_model and cur_model not in presets:
            presets.insert(0, cur_model)
        if presets:
            cmb_model.addItems(presets)
        if cur_model:
            cmb_model.setCurrentText(cur_model)
        cmb_model.setToolTip("下拉选择预设模型，也可直接输入任意模型名")
        model_row.addWidget(cmb_model, 1)
        root.addLayout(model_row)

        # ---- 模型预设列表 ----
        presets_label = QLabel("模型预设列表（与上方下拉框实时联动，每行一个）:")
        root.addWidget(presets_label)
        edit_presets = QTextEdit()
        edit_presets.setAcceptRichText(False)
        edit_presets.setPlainText("\n".join(cfg.get("model_presets") or []))
        edit_presets.setMaximumHeight(120)
        edit_presets.setPlaceholderText("每行一个模型名，修改后会自动同步到上方下拉框")
        root.addWidget(edit_presets)

        def _sync_presets_to_combo():
            """把预设文本框的内容同步到模型下拉框，保留当前选中项。"""
            new_list = [x.strip()
                        for x in edit_presets.toPlainText().splitlines() if x.strip()]
            cur = cmb_model.currentText().strip()
            cmb_model.blockSignals(True)
            cmb_model.clear()
            if new_list:
                cmb_model.addItems(new_list)
            if cur:
                # 若当前项在列表中则选中；否则追加作为可编辑值保留
                idx = cmb_model.findText(cur)
                if idx >= 0:
                    cmb_model.setCurrentIndex(idx)
                else:
                    cmb_model.setEditText(cur)
            cmb_model.blockSignals(False)

        edit_presets.textChanged.connect(_sync_presets_to_combo)

        # ---- 提供商切换：自动填充 base_url / 模型列表 ----
        def _on_provider_changed(idx):
            key = cmb_provider.itemData(idx)
            preset = AI_PROVIDER_PRESETS.get(key, {})
            if not preset:
                return
            # 只有 base_url 等于当前提供商的默认值或为空时才自动填充
            cur_base = edit_base.text().strip()
            prev_preset = AI_PROVIDER_PRESETS.get(current_provider, {})
            if not cur_base or cur_base == prev_preset.get("base_url", ""):
                edit_base.setText(preset.get("base_url", ""))
            # 切换模型预设列表
            new_models = preset.get("models", [])
            edit_presets.setPlainText("\n".join(new_models))
            cmb_model.clear()
            if new_models:
                cmb_model.addItems(new_models)
            default_model = preset.get("model", "")
            if default_model:
                cmb_model.setCurrentText(default_model)
            # 若 API Key 为空或是旧提供商的默认值，自动填充新默认值
            cur_key = edit_key.text().strip()
            if not cur_key or cur_key == prev_preset.get("default_key", ""):
                edit_key.setText(preset.get("default_key", ""))

        cmb_provider.currentIndexChanged.connect(_on_provider_changed)

        # ---- 对话测试组件 ----
        test_group = QGroupBox("对话测试（测试当前表单中的配置，无需先保存）")
        test_group.setStyleSheet("QGroupBox { font-weight: bold; margin-top: 8px; } "
                                 "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }")
        test_lay = QVBoxLayout(test_group)

        prompt_row = QHBoxLayout()
        edit_test_prompt = QLineEdit()
        edit_test_prompt.setPlaceholderText("输入测试提问，例如：你好，请用一句话介绍自己")
        edit_test_prompt.setText("你好，请用一句话介绍自己")
        prompt_row.addWidget(QLabel("提问:"), 0)
        prompt_row.addWidget(edit_test_prompt, 1)
        btn_test_chat = QPushButton("发送测试")
        btn_test_chat.setMinimumWidth(90)
        btn_test_chat.setToolTip(
            "用当前表单中的 provider/base_url/api_key/model 发一次请求")
        prompt_row.addWidget(btn_test_chat, 0)
        test_lay.addLayout(prompt_row)

        edit_test_result = QTextEdit()
        edit_test_result.setReadOnly(True)
        edit_test_result.setPlaceholderText("结果将在这里显示…")
        edit_test_result.setMinimumHeight(110)
        edit_test_result.setMaximumHeight(180)
        test_lay.addWidget(edit_test_result)

        root.addWidget(test_group)

        def _on_test_chat():
            """在后台线程用表单当前配置调用 AI，结果通过信号回主线程渲染。"""
            base_url = edit_base.text().strip().rstrip("/")
            api_key = edit_key.text().strip()
            model = cmb_model.currentText().strip()
            provider = cmb_provider.currentData()
            prompt = edit_test_prompt.text().strip()
            if not base_url:
                edit_test_result.setPlainText("[错误] Base URL 为空")
                return
            if not api_key:
                edit_test_result.setPlainText("[错误] API Key 为空")
                return
            if not model:
                edit_test_result.setPlainText("[错误] 未选择模型")
                return
            if not prompt:
                edit_test_result.setPlainText("[错误] 未输入提问")
                return
            if base_url and not base_url.endswith("/chat/completions"):
                chat_url = f"{base_url}/chat/completions"
            else:
                chat_url = base_url

            btn_test_chat.setEnabled(False)
            btn_test_chat.setText("请求中...")
            edit_test_result.setPlainText(
                f"[请求] provider={provider}\n"
                f"        url={chat_url}\n"
                f"        model={model}\n"
                f"        prompt={prompt}\n\n"
                "等待响应…"
            )

            bridge = _NetTestBridge()  # 复用 (ok, msg) 信号签名

            def _worker():
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "你是一个有用的助手"},
                        {"role": "user", "content": prompt},
                    ],
                }
                req = urllib.request.Request(
                    chat_url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                    },
                    method="POST",
                )
                t0 = time.time()
                try:
                    with _siliconflow_urlopen(req, 30) as resp:
                        text = resp.read().decode("utf-8", errors="replace")
                    data = json.loads(text)
                    choices = data.get("choices") or []
                    content = ""
                    if choices:
                        content = (
                            choices[0].get("message", {}).get("content", "")
                            or choices[0].get("delta", {}).get("content", "")
                        ).strip()
                    usage = data.get("usage") or {}
                    elapsed = time.time() - t0
                    if not content:
                        bridge.finished.emit(
                            False, f"AI 返回为空\n\n原始响应:\n{text[:800]}")
                    else:
                        usage_str = (
                            f"tokens: prompt={usage.get('prompt_tokens', '?')} "
                            f"completion={usage.get('completion_tokens', '?')} "
                            f"total={usage.get('total_tokens', '?')}"
                        )
                        bridge.finished.emit(
                            True,
                            f"[成功] {elapsed:.2f}s  {usage_str}\n\n{content}",
                        )
                except urllib.error.HTTPError as e:
                    body = ""
                    try:
                        body = e.read().decode("utf-8", errors="replace")[:600]
                    except Exception:
                        pass
                    bridge.finished.emit(
                        False, f"HTTPError {e.code}\n\n{body}")
                except Exception as exc:
                    bridge.finished.emit(False, f"{type(exc).__name__}: {exc}")

            def _on_result(ok, message):
                btn_test_chat.setEnabled(True)
                btn_test_chat.setText("发送测试")
                edit_test_result.setPlainText(message)
                if ok:
                    self._log(f"[ok] AI 模型测试成功: {model} @ {provider}")
                else:
                    self._log(
                        f"[ERR] AI 模型测试失败: {model} -> {message.splitlines()[0]}")

            bridge.finished.connect(_on_result)
            # 避免 bridge 被 GC，挂到 dlg 上
            dlg._chat_bridge = bridge  # type: ignore[attr-defined]
            self._start_worker(_worker)

        btn_test_chat.clicked.connect(_on_test_chat)

        # ---- 底部按钮 ----
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_save = QPushButton("保存")
        btn_cancel = QPushButton("取消")
        btn_row.addWidget(btn_save)
        btn_row.addWidget(btn_cancel)
        root.addLayout(btn_row)

        def _on_save():
            try:
                provider = cmb_provider.currentData()
                new_presets = [
                    x.strip() for x in edit_presets.toPlainText().splitlines() if x.strip()
                ]
                payload = {
                    "enabled": bool(chk_enabled.isChecked()),
                    "provider": provider,
                    "base_url": edit_base.text().strip(),
                    "api_key": edit_key.text().strip(),
                    "model": cmb_model.currentText().strip(),
                    "model_presets": new_presets,
                }
                _save_ai_config(payload)
                self._ai_config = payload
                self._refresh_model_edit_from_config()
                self._log(
                    f"[ok] AI 模型配置已保存: provider={provider} "
                    f"model={payload['model']} enabled={payload['enabled']}"
                )
                QMessageBox.information(
                    self,
                    "保存成功",
                    f"AI 模型配置已保存到:\n{AI_CONFIG_FILE}\n\n"
                    f"提供商: {provider}\n"
                    f"模型: {payload['model']}\n"
                    f"Base URL: {payload['base_url']}",
                )
                dlg.accept()
            except Exception as exc:
                self._log(f"[ERR] 保存 AI 配置失败: {exc}")
                QMessageBox.warning(self, "保存失败", str(exc))

        btn_save.clicked.connect(_on_save)
        btn_cancel.clicked.connect(dlg.reject)

        dlg.exec()

    def _check_github_auth(self):
        ok, msg = github_helper.check_auth(self._github_token())
        if ok:
            QMessageBox.information(self, "GitHub 授权状态", msg or "已授权。")
        else:
            QMessageBox.warning(self, "GitHub 授权状态", msg or "未授权。")

    @staticmethod
    def _probe_tcp(host: str, port: int, timeout_sec: float) -> tuple[bool, int, str]:
        start = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout_sec):
                pass
            return True, int((time.perf_counter() - start) * 1000), ""
        except OSError as exc:
            return False, int((time.perf_counter() - start) * 1000), str(exc)

    def _collect_proxy_info(self) -> list[str]:
        lines: list[str] = []
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            val = (os.environ.get(key) or "").strip()
            if val:
                lines.append(f"  {key}={val}")
        if not lines:
            lines.append("  (未设置 HTTP/HTTPS/ALL_PROXY 环境变量)")
        for cfg in ("http.proxy", "https.proxy"):
            val = git_helper.global_config_get(cfg)
            if val:
                lines.append(f"  git global {cfg}={val}")
        return lines

    @staticmethod
    def _is_transient_git_ssl_error(msg: str) -> bool:
        low = (msg or "").lower()
        return any(
            m in low
            for m in (
                "ssl_error_syscall",
                "ssl_read",
                "connection reset",
                "connection aborted",
                "eof",
            )
        )

    def _probe_git_ls_remote_once(
        self, repo_url: str, timeout_sec: int = 30
    ) -> tuple[bool, int, str]:
        start = time.perf_counter()
        ok, detail = git_helper.ls_remote(
            repo_url, "HEAD", timeout_sec=timeout_sec)
        ms = int((time.perf_counter() - start) * 1000)
        if ok:
            return True, ms, detail
        if "timeout" in (detail or "").lower() or "timed out" in (detail or "").lower():
            return False, timeout_sec * 1000, f"超时 ({timeout_sec}s)"
        return False, ms, detail

    def _probe_git_ls_remote(
        self, repo_url: str, timeout_sec: int = 30, retries: int = 3
    ) -> tuple[bool, int, str, int]:
        """探测 Git HTTPS；返回 (ok, ms, detail, attempts)。"""
        last_ms = 0
        last_detail = ""
        attempts = 0
        for attempt in range(retries):
            attempts = attempt + 1
            if attempt > 0:
                time.sleep(1.0)
            ok, last_ms, last_detail = self._probe_git_ls_remote_once(
                repo_url, timeout_sec
            )
            if ok:
                if attempt > 0:
                    return True, last_ms, f"(第{attempts}次成功) {last_detail}", attempts
                return True, last_ms, last_detail, attempts
            if attempt + 1 >= retries or not self._is_transient_git_ssl_error(
                last_detail
            ):
                break
        return False, last_ms, last_detail, attempts

    def _test_network_connection(self):
        if self._net_test_running:
            return
        ok_git, msg_git = git_helper.check_git_available()
        if not ok_git:
            QMessageBox.warning(
                self, "网络测试", f"Git 不可用（GitPython）:\n{msg_git}")
            return
        self._net_test_running = True
        self.btn_test_network.setEnabled(False)
        owner = self.owner_edit.text().strip() or "Lugwit123"
        self._log("========== 测试网络连接 ==========")

        def _worker():
            lines = ["[代理配置]", *self._collect_proxy_info(), ""]
            ok_tcp, ms_tcp, err_tcp = self._probe_tcp("github.com", 443, 5.0)
            lines.append(
                f"[TCP] github.com:443 -> {'OK' if ok_tcp else 'FAIL'} ({ms_tcp} ms)"
            )
            if err_tcp:
                lines.append(f"  {err_tcp}")
            http_ver = self._get_configured_http_version()
            if http_ver:
                lines.append(f"[Git] 使用 http.version={http_ver}（来自网络设置）")
            else:
                lines.append("[Git] 使用默认 http.version（由 git 自动选择）")
            lines.append("")
            git_ok = True
            for idx, pkg in enumerate(("l_WChat", "l_repo_sync_gui")):
                if idx > 0:
                    time.sleep(0.5)
                url = f"https://github.com/{owner}/{pkg}.git"
                ok_git, ms_git, detail, attempts = self._probe_git_ls_remote(
                    url)
                git_ok = git_ok and ok_git
                status = "OK" if ok_git else "FAIL"
                lines.append(
                    f"[Git] ls-remote {pkg} -> {status} ({ms_git} ms, {attempts} 次)"
                )
                if detail:
                    lines.append(f"  {detail}")
            all_ok = ok_tcp and git_ok
            self._net_test_bridge.finished.emit(all_ok, "\n".join(lines))

        self._start_worker(_worker)

    def _on_network_test_finished(self, all_ok: bool, report: str):
        self._net_test_running = False
        self.btn_test_network.setEnabled(True)
        for line in report.splitlines():
            self._log(line)

        dlg = QMessageBox(self)
        dlg.setWindowTitle("网络连接测试")
        dlg.setIcon(QMessageBox.Information if all_ok else QMessageBox.Warning)
        text = report
        if not all_ok:
            self._log("[ERR] 网络连接测试存在失败项")
            text += (
                "\n\n部分检测失败。若仅个别仓库偶发 SSL_ERROR_SYSCALL，"
                "多为间歇性连接问题，请重试测试或上传。"
            )
        else:
            self._log("[ok] 网络连接测试全部通过")
        dlg.setText(text)
        dlg.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)

        btn_copy = dlg.addButton("复制结果", QMessageBox.ActionRole)
        btn_close = dlg.addButton("关闭", QMessageBox.AcceptRole)
        dlg.setDefaultButton(btn_close)

        # 循环显示，支持多次点击「复制结果」
        while True:
            dlg.exec()
            clicked = dlg.clickedButton()
            if clicked is btn_copy:
                try:
                    QApplication.clipboard().setText(report)
                    self._log("[ok] 测试报告已复制到剪贴板")
                    # 短暂改变按钮文案给予反馈
                    btn_copy.setText("✓ 已复制")
                    # 延时后恢复按钮文案（使用 QTimer，避免 sleep 阻塞主线程）
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(1500, lambda: btn_copy.setText("复制结果"))
                except Exception as exc:
                    self._log(f"[ERR] 复制失败: {exc}")
                continue
            # 点击「关闭」或其他按钮则退出
            break

    def _start_gh_auth_login(self):
        """打开 GitHub Token 创建页，引导用户粘贴 Token。"""
        try:
            webbrowser.open(github_helper.TOKEN_CREATE_URL)
        except Exception as exc:
            self._log(f"[ERR] 打开浏览器失败: {exc}")
        QMessageBox.information(
            self,
            "GitHub 授权",
            (
                "已尝试打开 GitHub Token 创建页面。\n\n"
                "1) 生成 Personal Access Token（需 repo 权限）\n"
                "2) 复制 Token 粘贴到上方「GitHub Token」输入框\n"
                "3) 点击「保存 Token」\n"
                "4) 点击「授权状态」验证\n\n"
                f"链接:\n{github_helper.TOKEN_CREATE_URL}"
            ),
        )
        self._log("[info] 已打开 GitHub Token 创建页面，请粘贴 Token 并保存。")

    def _repo(self, pkg_dir: Path) -> Repo | None:
        return git_helper.open_repo(pkg_dir)

    def _list_scroll_pos(self) -> int:
        bar = self.list_area.verticalScrollBar()
        return bar.value() if bar is not None else 0

    def _restore_list_scroll(self, pos: int):
        bar = self.list_area.verticalScrollBar()
        if bar is not None:
            bar.setValue(pos)

    def _set_busy(self, busy: bool):
        scroll = self._list_scroll_pos()
        for btn in self.row_buttons:
            btn.setEnabled(not busy)
        if self.btn_refresh_status is not None:
            self.btn_refresh_status.setEnabled(not busy)
        self._restore_list_scroll(scroll)

    def _set_package_refresh_busy(self, busy: bool):
        self._package_refresh_running = busy
        self._set_busy(busy)
        if self.btn_refresh_packages is not None:
            self.btn_refresh_packages.setEnabled(not busy)

    def _start_worker(self, fn):
        """在 Qt 托管线程池中运行后台任务（替代裸 threading.Thread）。"""
        QThreadPool.globalInstance().start(_FnRunnable(fn))

    def _post_main(self, fn):
        """把 fn 调度到主线程执行，供 worker 线程安全更新 UI（控件/弹窗）。"""
        if QThread.currentThread() is self.thread():
            fn()
        else:
            self._main_invoker.post(fn)

    def _run_blocking_with_events(self, func, *args, **kwargs):
        """同步执行阻塞函数（git clone/commit/push 等子进程操作）。

        历史教训（fault 日志 11 次崩溃均指向同一处）：
        曾用「后台 QThreadPool 线程 + 主线程嵌套 QEventLoop 等待」的方案，
        想在等 git 时保持窗口响应。但主线程在嵌套事件循环里会**重入**处理：
          - 原生 Windows 消息 → tray_window.nativeEvent
          - 跨线程投递的回调 → _MainThreadInvoker._invoke
        并在后台 worker 线程销毁其 Qt 线程数据时触发
        "QThreadStorage: entry destroyed before end of thread" → abort()，
        且该 abort 来自 C++ 层，Python try/except 无法捕获。

        这里包裹的全部是数秒级的 git 子进程调用（无长任务，AI 走 urllib
        不经此函数），因此改为**纯同步执行**：不起 worker 线程、不泵嵌套
        事件循环。代价是 git 执行期间窗口短暂无响应，但彻底消除崩溃路径。
        """
        return func(*args, **kwargs)

    def _run_quiet(
        self,
        cmd: list[str],
        cwd: Path | None = None,
        safe_dir: Path | None = None,
        *,
        timeout: float = 120,
    ) -> tuple[bool, str]:
        """后台线程用：不写日志、不 pump 事件循环。"""
        if not cmd:
            return False, "empty command"

        if cmd[0] == "git":
            repo_safe = safe_dir if safe_dir is not None else cwd
            git_args = cmd[1:]
            if len(git_args) >= 2 and git_args[0] == "clone":
                return git_helper.clone_from(
                    git_args[1],
                    Path(git_args[2]),
                    safe_dir=repo_safe,
                    timeout=timeout,
                )
            return git_helper.execute_git(
                git_args,
                cwd=cwd,
                repo_dir=repo_safe,
                timeout=timeout,
            )

        return False, f"unsupported command: {cmd[0]}"

    def _log(self, text: str):
        """输出日志到 GUI、终端和日志文件。

        可从任意线程调用：非主线程时自动 marshal 回主线程更新 GUI 控件，
        避免跨线程访问 QPlainTextEdit 导致崩溃。
        """
        if QThread.currentThread() is not self.thread():
            self._post_main(lambda: self._log(text))
            return
        now = datetime.datetime.now().strftime("%H:%M:%S")
        log_line = f"[{now}] {text}"

        # 1. 输出到 GUI 日志组件（CodeEditorWidget 内部为 QPlainTextEdit）
        scroll = self._list_scroll_pos()
        inner = self.log_edit.editor()
        inner.appendPlainText(log_line)
        inner.ensureCursorVisible()
        self._restore_list_scroll(scroll)

        # 2. 输出到终端
        print(log_line, flush=True)

        # 3. 写入日志文件
        if hasattr(self, "_log_file_handler") and self._log_file_handler:
            try:
                self._log_file_handler.write(log_line + "\n")
                self._log_file_handler.flush()
            except Exception:
                pass

    def _init_log_file(self):
        """初始化日志文件，按日期创建。"""
        try:
            log_dir = Path(os.environ.get("TEMP", ".")) / \
                "l_repo_sync_gui" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            log_path = log_dir / f"sync_{today}.log"
            self._log_file_handler = open(log_path, "a", encoding="utf-8")
            self._log(f"日志文件: {log_path}")
        except Exception as exc:
            self._log_file_handler = None
            print(f"[WARN] 无法创建日志文件: {exc}", file=sys.stderr)

    def _log_phase_start(self, phase: str) -> str:
        """记录阶段开始，返回 phase key 用于结束时计算耗时。"""
        key = phase
        if not hasattr(self, "_phase_timers"):
            self._phase_timers: dict[str, float] = {}
        self._phase_timers[key] = time.time()
        self._log(f"\u25b6 {phase}")
        return key

    def _log_phase_end(self, phase: str, *, ok: bool = True):
        """记录阶段结束，自动计算并显示耗时。"""
        elapsed = ""
        if hasattr(self, "_phase_timers") and phase in self._phase_timers:
            dt = time.time() - self._phase_timers.pop(phase)
            if dt >= 60:
                elapsed = f" ({dt:.0f}s)"
            else:
                elapsed = f" ({dt:.2f}s)"
        icon = "\u2713" if ok else "\u2717"
        self._log(f"{icon} {phase}{elapsed}")

    def _get_configured_http_version(self) -> str:
        """从网络配置文件中读取用户设置的 http.version（空字符串表示不强制）。"""
        try:
            if not NET_CONFIG_FILE.exists():
                return ""
            data = json.loads(NET_CONFIG_FILE.read_text(encoding="utf-8"))
            if not data.get("enabled", False):
                return ""
            return (data.get("http_version") or "").strip()
        except Exception:
            return ""

    def _run(
        self,
        cmd: list[str],
        cwd: Path | None = None,
        safe_dir: Path | None = None,
    ) -> tuple[bool, str]:
        if not cmd:
            return False, "empty command"

        if cmd[0] == "git":
            repo_safe = safe_dir if safe_dir is not None else cwd
            git_args = cmd[1:]
            self._log(f"$ git {' '.join(git_args)}")
            if len(git_args) >= 2 and git_args[0] == "clone":
                return self._run_blocking_with_events(
                    git_helper.clone_from,
                    git_args[1],
                    Path(git_args[2]),
                    safe_dir=repo_safe,
                )
            return self._run_blocking_with_events(
                git_helper.execute_git,
                git_args,
                cwd=cwd,
                repo_dir=repo_safe,
            )

        return False, f"unsupported command: {cmd[0]}"

    def _check_tools(self) -> bool:
        ok_git, msg_git = git_helper.check_git_available()
        if not ok_git:
            QMessageBox.warning(
                self,
                "缺少工具",
                (
                    "GitPython 无法调用 git（请确认 Git 已安装且在 PATH 中）。\n\n"
                    f"详细信息:\n{msg_git}"
                ),
            )
            return False
        if not self._github_token():
            self._log("[WARN] 未配置 GitHub Token，远端列表/建仓/删远端等功能将不可用。")
        return True

    def _local_package_entries(self) -> list[tuple[str, Path]]:
        """本地 rez 包目录（含 wuwo）。"""
        out: list[tuple[str, Path]] = []
        for item in sorted(self.rez_source.iterdir(), key=lambda p: p.name.lower()):
            if not item.is_dir():
                continue
            if item.name in SKIP_DIRS:
                continue
            if not list(item.glob("*/package.py")):
                continue
            out.append((item.name, item))

        wuwo_dir = self.rez_source.parent / "wuwo"
        if wuwo_dir.is_dir():
            out.append(("wuwo", wuwo_dir))
        return out

    def _remote_package_path(self, name: str) -> Path:
        if name == "wuwo":
            return self.rez_source.parent / "wuwo"
        return self.rez_source / name

    def _is_managed_package_dir(self, pkg_dir: Path) -> bool:
        """限制删除范围：rez-package-source 下包目录或 wuwo。"""
        resolved = pkg_dir.resolve()
        wuwo_dir = (self.rez_source.parent / "wuwo").resolve()
        if resolved == wuwo_dir:
            return True
        try:
            resolved.relative_to(self.rez_source.resolve())
            return True
        except ValueError:
            return False

    def _fetch_remote_repo_names(self, owner: str) -> tuple[bool, list[str], str]:
        """列出 GitHub owner 下全部仓库名。"""
        return github_helper.list_repo_names(owner, self._github_token())

    def _fetch_remote_repo_names_quiet(self, owner: str) -> tuple[bool, list[str], str]:
        return github_helper.list_repo_names(owner, self._github_token())

    def _collect_full_package_data(
        self,
        owner: str,
        on_entry_ready=None,
    ) -> tuple[list[tuple[str, Path, bool]], dict, list[str]]:
        """后台收集包列表与 git 状态。"""
        warnings: list[str] = []
        local = self._local_package_entries()
        local_names = {name for name, _ in local}
        entries: list[tuple[str, Path, bool]] = [
            (name, path, False) for name, path in local
        ]

        self._package_refresh_bridge.log.emit("获取远端仓库列表…")
        ok, remote_names, err = self._fetch_remote_repo_names_quiet(owner)
        if ok:
            self._package_refresh_bridge.log.emit(
                f"远端仓库数: {len(remote_names)}")
            for name in sorted(set(remote_names), key=str.lower):
                if name in local_names or name in SKIP_DIRS:
                    continue
                entries.append((name, self._remote_package_path(name), True))
        elif self._github_token():
            warnings.append(f"[WARN] 获取远端仓库列表失败，仅显示本地包: {err}")

        return entries, {}, warnings

    def _package_entries(self) -> list[tuple[str, Path, bool]]:
        """本地包 + 远端有但本地无的仓库。"""
        local = self._local_package_entries()
        local_names = {name for name, _ in local}
        entries: list[tuple[str, Path, bool]] = [
            (name, path, False) for name, path in local]

        owner = self.owner_edit.text().strip() or "Lugwit123"
        ok, remote_names, err = self._fetch_remote_repo_names(owner)
        if ok:
            for name in sorted(set(remote_names), key=str.lower):
                if name in local_names or name in SKIP_DIRS:
                    continue
                entries.append((name, self._remote_package_path(name), True))
        elif self._github_token():
            self._log(f"[WARN] 获取远端仓库列表失败，仅显示本地包: {err}")

        return entries

    @staticmethod
    def _parse_porcelain_line(line: str) -> tuple[str, str] | None:
        """解析 git status --porcelain 单行，返回 (XY, path)。"""
        line = (line or "").rstrip()
        if not line:
            return None
        if line.startswith("?? "):
            return "??", line[3:]
        if len(line) < 3:
            return line[:2], ""
        xy = line[:2]
        # XY 后若第 3 列是空格则为分隔符；否则 Y 为空白时路径紧跟在 index 2（如 "M wuwo.bat"）。
        path_part = line[3:] if line[2] == " " else line[2:]
        return xy, path_part

    @staticmethod
    def _is_diverged(sync: dict[str, int] | None) -> bool:
        sync = sync or {}
        return sync.get("ahead", 0) > 0 and sync.get("behind", 0) > 0

    @staticmethod
    def _read_text_file(path: Path, max_bytes: int = 512_000) -> str | None:
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
            if len(data) > max_bytes or b"\x00" in data[:8192]:
                return None
            return data.decode("utf-8")
        except Exception:
            return None

    def _git_name_only_set(self, pkg_dir: Path, rev_range: str) -> set[str]:
        repo = self._repo(pkg_dir)
        if repo is None:
            return set()
        try:
            return {str(path).replace("\\", "/") for path in repo.git.diff("--name-only", rev_range).splitlines() if str(path).strip()}
        except Exception:
            return set()

    def _uncommitted_path_set(self, pkg_dir: Path) -> set[str]:
        ok, status_lines = self._status_lines(pkg_dir)
        paths: set[str] = set()
        if not ok:
            return paths
        for raw in status_lines:
            parsed = self._parse_porcelain_line(raw)
            if not parsed:
                continue
            _, path_part = parsed
            if not path_part:
                continue
            path = path_part.split(" -> ")[-1].strip().replace("\\", "/")
            if path:
                paths.add(path)
        return paths

    def _upstream_ref(self, pkg_dir: Path) -> str | None:
        repo = self._repo(pkg_dir)
        if repo is None:
            return None
        try:
            tracking = repo.active_branch.tracking_branch()
            return str(tracking) if tracking is not None else None
        except Exception:
            return None

    def _analyze_merge_plan(self, pkg_dir: Path) -> tuple[dict | None, str]:
        """按 merge-base 划分：本地独有 / 远端独有 / 双方均改。"""
        repo = self._repo(pkg_dir)
        if repo is None:
            return None, "非git仓库"
        try:
            try:
                repo.remotes.origin.fetch(prune=True)
            except Exception:
                pass
            upstream = self._upstream_ref(pkg_dir)
            if not upstream:
                return None, "无上游分支"
            merge_base = repo.git.merge_base(
                "HEAD", upstream).strip().splitlines()[-1]
            local_files = self._git_name_only_set(
                pkg_dir, f"{merge_base}..HEAD")
            local_files |= self._uncommitted_path_set(pkg_dir)
            remote_files = self._git_name_only_set(
                pkg_dir, f"{merge_base}..{upstream}")
            local_only = sorted(local_files - remote_files)
            remote_only = sorted(remote_files - local_files)
            both = sorted(local_files & remote_files)
            return {
                "upstream": upstream,
                "merge_base": merge_base,
                "local_only": local_only,
                "remote_only": remote_only,
                "both": both,
            }, ""
        except Exception as exc:
            return None, str(exc)

    def _preview_ai_merge(self, pkg_dir: Path) -> tuple[list[str], dict | None, str]:
        plan, err = self._analyze_merge_plan(pkg_dir)
        if plan is None:
            return [f"[无法分析] {err}"], None, err
        lines = [
            f"[AI 合并预览] merge-base={plan['merge_base'][:8]}… upstream={plan['upstream']}",
            "",
            f"[保留本地] 仅本地改动 ({len(plan['local_only'])}):",
        ]
        lines.extend(plan["local_only"][:40] or ["(无)"])
        if len(plan["local_only"]) > 40:
            lines.append(f"...(共 {len(plan['local_only'])} 个)")
        lines.extend(["", f"[取远端] 仅远端改动 ({len(plan['remote_only'])}):"])
        lines.extend(plan["remote_only"][:40] or ["(无)"])
        if len(plan["remote_only"]) > 40:
            lines.append(f"...(共 {len(plan['remote_only'])} 个)")
        lines.extend(["", f"[AI 合并] 双方均改 ({len(plan['both'])}):"])
        lines.extend(plan["both"][:40] or ["(无)"])
        if len(plan["both"]) > 40:
            lines.append(f"...(共 {len(plan['both'])} 个)")

        # 追加详细 diff 预览，供确认弹窗按文件标签页展示。
        # 使用 git diff HEAD..upstream 的 patch 输出，让 _confirm_action 中的
        # _extract_file_diff_blocks() 能按 diff --git 块拆分为「一文件一标签页」。
        upstream = plan.get("upstream")
        if upstream:
            lines.append("")
            lines.extend(
                self._diff_lines(
                    pkg_dir,
                    ["git", "diff", "--patch", f"HEAD..{upstream}"],
                    "AI 合并 diff 预览",
                )
            )
        return lines, plan, ""

    def _git_show_text(self, pkg_dir: Path, ref: str, relpath: str) -> str | None:
        repo = self._repo(pkg_dir)
        if repo is None:
            return None
        try:
            blob = repo.commit(ref).tree / relpath
            data = blob.data_stream.read()
            if b"\x00" in data:
                return None
            return data.decode("utf-8")
        except Exception:
            return None

    def _read_local_merge_text(self, pkg_dir: Path, relpath: str) -> str | None:
        disk = self._read_text_file(pkg_dir / relpath)
        if disk is not None:
            return disk
        return self._git_show_text(pkg_dir, "HEAD", relpath)

    # ---- AI 配置辅助方法 ----
    def _refresh_model_edit_from_config(self):
        """根据 _ai_config 更新模型下拉框列表与当前选项。"""
        cfg = getattr(self, "_ai_config", None) or {}
        model_edit = self.model_edit
        if not model_edit:
            return
        model_edit.clear()
        presets = cfg.get("model_presets") or []
        current = (cfg.get("model") or "").strip()
        if current and current not in presets:
            presets = [current, *presets]
        if presets:
            model_edit.addItems(presets)
        if current:
            model_edit.setCurrentText(current)
        model_edit.setToolTip(f"当前模型: {current or '(未设置)'}\n点击“模型设置…”修改")

    def _get_ai_endpoint(self) -> tuple[str, str, str]:
        """从 _ai_config 获取 (chat_url, api_key, model)。"""
        cfg = getattr(self, "_ai_config", None) or {}
        base_url = (cfg.get("base_url") or "").rstrip("/")
        api_key = (cfg.get("api_key") or "").strip()
        model = (cfg.get("model") or "").strip()
        if base_url and not base_url.endswith("/chat/completions"):
            chat_url = f"{base_url}/chat/completions"
        else:
            chat_url = base_url or SILICONFLOW_URL
        return chat_url, api_key, model

    def _call_ai_text(
        self, system: str, user: str, timeout: float = 90
    ) -> tuple[bool, str]:
        """通用 OpenAI 兼容 API 调用（支持 SiliconFlow / Zhipu / DeepSeek / 自定义）。"""
        cfg = getattr(self, "_ai_config", None) or {}
        if not cfg.get("enabled", True):
            return False, "AI 模型已禁用（在“模型设置…”中启用）"
        chat_url, api_key, model = self._get_ai_endpoint()
        if not api_key:
            return False, "未填写 AI API Key（在“模型设置…”中填写）"
        if not chat_url:
            return False, "未配置 Base URL（在“模型设置…”中填写）"
        if not model:
            return False, "未选择模型（在“模型设置…”中选择）"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        req = urllib.request.Request(
            chat_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            # 国内提供商直连，避免误走系统代理
            with _siliconflow_urlopen(req, timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            data = json.loads(text)
            choices = data.get("choices") or []
            content = ""
            if choices:
                content = (
                    choices[0].get("message", {}).get("content", "")
                    or choices[0].get("delta", {}).get("content", "")
                ).strip()
            if not content:
                return False, f"AI 返回为空: {text[:200]}"
            return True, content
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            return False, f"HTTPError {e.code}: {body}"
        except Exception as exc:
            return False, str(exc)

    def _call_siliconflow_text(
        self, system: str, user: str, timeout: float = 90
    ) -> tuple[bool, str]:
        # 保留兼容，内部转发到统一 _call_ai_text
        return self._call_ai_text(system, user, timeout=timeout)

    def _ai_merge_file_content(
        self,
        relpath: str,
        local_text: str,
        remote_text: str,
        base_text: str = "",
    ) -> tuple[bool, str]:
        local_clip = local_text[:AI_MERGE_MAX_FILE_CHARS]
        remote_clip = remote_text[:AI_MERGE_MAX_FILE_CHARS]
        base_clip = (base_text or "")[:AI_MERGE_MAX_FILE_CHARS]
        base_section = f"[共同祖先]\n{base_clip}\n\n" if base_clip else ""
        prompt = (
            f"文件路径: {relpath}\n\n"
            "请合并「本地版」与「远端版」，输出合并后的完整文件内容。\n"
            "规则：\n"
            "1) 仅本地有的改动保留；仅远端有的改动采纳；双方都改的部分综合两者意图。\n"
            "2) 只输出合并后的文件全文，不要解释、不要 markdown 代码块。\n"
            "3) 保持原文件语言与风格。\n\n"
            f"{base_section}"
            f"[本地版]\n{local_clip}\n\n"
            f"[远端版]\n{remote_clip}"
        )
        ok, content = self._call_siliconflow_text(
            "你是代码合并专家，擅长三方合并并输出可直接保存的完整文件。",
            prompt,
            timeout=120,
        )
        if not ok:
            return False, content
        if content.startswith("```"):
            lines = content.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines).strip()
        return True, content

    def _write_merged_file(self, pkg_dir: Path, relpath: str, content: str) -> bool:
        target = pkg_dir / relpath
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            return True
        except Exception as exc:
            self._log(f"[ERR] 写入 {relpath} 失败: {exc}")
            return False

    def _merge_head_exists(self, pkg_dir: Path) -> bool:
        repo = self._repo(pkg_dir)
        if repo is None:
            return False
        try:
            return (pkg_dir / ".git" / "MERGE_HEAD").exists()
        except Exception:
            return False

    def _abort_merge_if_needed(self, pkg_dir: Path):
        repo = self._repo(pkg_dir)
        if repo is None:
            return
        try:
            repo.git.merge("--abort")
        except Exception:
            pass

    def _git_show_stage(self, pkg_dir: Path, stage: int, relpath: str) -> str | None:
        repo = self._repo(pkg_dir)
        if repo is None:
            return None
        try:
            blob = repo.git.show(f":{stage}:{relpath}")
            if not blob or "\x00" in blob:
                return None
            return blob
        except Exception:
            return None

    def _read_merge_versions(
        self, pkg_dir: Path, upstream: str, merge_base: str, relpath: str
    ) -> tuple[str | None, str | None, str]:
        base = self._git_show_stage(pkg_dir, 1, relpath)
        if base is None:
            base = self._git_show_text(pkg_dir, merge_base, relpath) or ""
        ours = self._git_show_stage(pkg_dir, 2, relpath)
        if ours is None:
            ours = self._read_local_merge_text(pkg_dir, relpath)
        theirs = self._git_show_stage(pkg_dir, 3, relpath)
        if theirs is None:
            theirs = self._git_show_text(pkg_dir, upstream, relpath)
        return ours, theirs, base

    def _checkout_merge_side(
        self, pkg_dir: Path, side: str, upstream: str, relpath: str
    ) -> tuple[bool, str]:
        repo = self._repo(pkg_dir)
        if repo is None:
            return False, "非git仓库"
        try:
            if side == "--ours":
                repo.git.checkout("--ours", "--", relpath)
            else:
                repo.git.checkout("--theirs", "--", relpath)
            return True, ""
        except Exception as exc:
            try:
                fallback_ref = "HEAD" if side == "--ours" else upstream
                repo.git.checkout(fallback_ref, "--", relpath)
                return True, ""
            except Exception as exc2:
                return False, str(exc2 or exc)

    def _execute_ai_merge(self, pkg_dir: Path, plan: dict) -> bool:
        upstream = plan["upstream"]
        merge_base = plan["merge_base"]
        pkg_name = pkg_dir.name

        self._abort_merge_if_needed(pkg_dir)
        ok_merge, msg_merge = self._run(
            ["git", "merge", upstream, "--no-commit", "--no-ff"], cwd=pkg_dir
        )
        if not self._merge_head_exists(pkg_dir) and not ok_merge:
            self._log(f"[ERR] {pkg_name} 无法开始 merge: {msg_merge}")
            QMessageBox.warning(self, "AI 合并失败", f"无法开始 merge:\n{msg_merge}")
            return False

        for relpath in plan["local_only"]:
            ok, msg = self._checkout_merge_side(
                pkg_dir, "--ours", upstream, relpath)
            if not ok:
                self._abort_merge_if_needed(pkg_dir)
                QMessageBox.warning(
                    self, "AI 合并失败", f"保留本地失败:\n{relpath}\n{msg}")
                return False
            self._log(f"[merge] {relpath} ← 保留本地")

        for relpath in plan["remote_only"]:
            ok, msg = self._checkout_merge_side(
                pkg_dir, "--theirs", upstream, relpath)
            if not ok:
                self._abort_merge_if_needed(pkg_dir)
                QMessageBox.warning(
                    self, "AI 合并失败", f"取远端失败:\n{relpath}\n{msg}")
                return False
            self._log(f"[merge] {relpath} ← 远端")

        for relpath in plan["both"]:
            local_text, remote_text, base_text = self._read_merge_versions(
                pkg_dir, upstream, merge_base, relpath
            )
            if local_text is None or remote_text is None:
                self._log(f"[WARN] {relpath} 无法文本合并，保留本地")
                self._checkout_merge_side(pkg_dir, "--ours", upstream, relpath)
                continue
            self._log(f"[merge] {relpath} … AI 合并中")
            ok, merged = self._ai_merge_file_content(
                relpath, local_text, remote_text, base_text)
            if not ok:
                self._abort_merge_if_needed(pkg_dir)
                QMessageBox.warning(self, "AI 合并失败", f"{relpath}:\n{merged}")
                return False
            if not self._write_merged_file(pkg_dir, relpath, merged):
                self._abort_merge_if_needed(pkg_dir)
                return False
            self._log(f"[merge] {relpath} ✓ AI 合并完成")

        repo = self._repo(pkg_dir)
        if repo is None:
            return False
        try:
            repo.git.add(A=True)
        except Exception as exc:
            self._abort_merge_if_needed(pkg_dir)
            self._log(f"[ERR] {pkg_name} add 失败: {exc}")
            QMessageBox.warning(self, "AI 合并失败", f"add 失败:\n{exc}")
            return False
        try:
            if not repo.is_dirty(index=True, working_tree=False, untracked_files=True) and not self._merge_head_exists(pkg_dir):
                self._log(f"[info] {pkg_name} 合并后无 staged 变更")
                return True
        except Exception:
            pass
        commit_msg = self._request_ai_commit_message(pkg_name, pkg_dir)
        if not commit_msg:
            commit_msg = (
                f"merge: 同步本地与远端（AI 合并 {len(plan['both'])} 个文件）"
            )
        try:
            repo.index.commit(commit_msg)
        except Exception as exc:
            self._abort_merge_if_needed(pkg_dir)
            self._log(f"[ERR] {pkg_name} 合并提交失败: {exc}")
            QMessageBox.warning(self, "AI 合并失败", f"提交失败:\n{exc}")
            return False
        self._log(f"[ok] {pkg_name} AI 合并已提交，可点「上传」推送")
        return True

    def merge_one_ai(self, pkg_dir: Path):
        pkg_name = pkg_dir.name
        if not self._check_tools():
            return
        sync = self.package_sync_map.get(pkg_name, {})
        if not self._is_diverged(sync):
            QMessageBox.information(
                self,
                "无需合并",
                f"{pkg_name} 当前不是「本地领先且远端领先」的分叉状态。",
            )
            return
        if not (self._ai_config or {}).get("enabled", True) or not (self._ai_config or {}).get("api_key", "").strip():
            QMessageBox.warning(self, "AI 合并", "请先在「模型设置…」中配置并启用 AI。")
            return
        self._set_busy(True)
        try:
            self._log(f"========== AI 合并 {pkg_name} ==========")
            preview_lines, plan, err = self._preview_ai_merge(pkg_dir)
            if plan is None:
                self._log(f"[ERR] {pkg_name} 无法分析: {err}")
                QMessageBox.warning(self, "AI 合并", err)
                return
            if not plan["remote_only"] and not plan["both"]:
                QMessageBox.information(
                    self,
                    "AI 合并",
                    f"{pkg_name} 无远端独有或双方均改的文件，可直接上传或下载。",
                )
                return
            confirmed, _ = self._confirm_action(
                "确认 AI 合并",
                pkg_name,
                preview_lines,
                "无文件需要合并",
                enable_ai=False,
                ai_merge_plan=plan,
                ai_merge_pkg_dir=pkg_dir,
            )
            if not confirmed:
                self._log(f"[info] {pkg_name} 取消 AI 合并")
                return
            if not self._execute_ai_merge(pkg_dir, plan):
                return
            QMessageBox.information(
                self,
                "AI 合并完成",
                f"{pkg_name} 已合并并提交。\n请点击「上传」推送到 GitHub。",
            )
        finally:
            self._set_busy(False)
            self._update_one_package_status(pkg_name, pkg_dir)

    @staticmethod
    def _summarize_status_counts(status_lines: list[str]) -> dict[str, int]:
        counts = {
            "modified": 0,
            "untracked": 0,
            "conflicted": 0,
            "deleted": 0,
            "renamed": 0,
            "staged": 0,
        }
        for raw in status_lines:
            line = (raw or "").rstrip()
            if not line:
                continue
            if line.startswith("?? "):
                counts["untracked"] += 1
                continue

            xy = line[:2]
            x = xy[0] if len(xy) > 0 else " "
            y = xy[1] if len(xy) > 1 else " "
            code_pair = {x, y}
            if "U" in code_pair:
                counts["conflicted"] += 1
                continue
            if x not in (" ", "?"):
                counts["staged"] += 1
            if "M" in code_pair:
                counts["modified"] += 1
            if "D" in code_pair:
                counts["deleted"] += 1
            if "R" in code_pair:
                counts["renamed"] += 1
        return counts

    def _collect_deletion_paths(self, pkg_dir: Path, status_lines: list[str]) -> list[str]:
        """从 git status --porcelain 提取本地已删除、上传时将提交删除的路径。"""
        paths: list[str] = []
        for raw in status_lines:
            parsed = self._parse_porcelain_line(raw)
            if not parsed:
                continue
            xy, path_part = parsed
            if "D" not in xy:
                continue
            if " -> " in path_part:
                rel = path_part.split(" -> ", 1)[0].strip().strip('"')
            else:
                rel = path_part.strip().strip('"')
            paths.append(str((pkg_dir / rel).resolve()))
        return paths

    @staticmethod
    def _is_deletion_line(line: str) -> bool:
        """判断预览/状态行是否表示文件删除（含 porcelain、name-status、带完整路径格式）。"""
        stripped = (line or "").strip()
        if not stripped or stripped.startswith("["):
            return False
        if "\t" in stripped:
            return stripped.split("\t", 1)[0].strip().upper() == "D"
        parsed = RepoSyncWindow._parse_porcelain_line(stripped)
        if parsed:
            xy, _ = parsed
            return "D" in xy
        parts = stripped.split(maxsplit=1)
        if not parts:
            return False
        status_token = parts[0]
        return len(status_token) <= 2 and "D" in status_token

    def _append_pending_deletion_notice(
        self, out: list[str], pkg_dir: Path, status_lines: list[str]
    ) -> None:
        deletions = self._collect_deletion_paths(pkg_dir, status_lines)
        if not deletions:
            return
        out.append("")
        out.append(
            f"[将提交的删除] 本地已删除 {len(deletions)} 个文件，上传后将同步删除远端："
        )
        for path in deletions:
            out.append(f"  D {path}")

    def _append_local_pending_preview(
        self, out: list[str], pkg_dir: Path, ok_status: bool, status_lines: list[str]
    ) -> None:
        local_pending = (
            self._format_status_lines_with_full_path(
                pkg_dir, status_lines) if ok_status else []
        )
        out.append("")
        out.append("[本地未提交改动]")
        out.extend(local_pending or ["(无本地未提交改动)"])
        if ok_status:
            self._append_pending_deletion_notice(out, pkg_dir, status_lines)
        local_diff = self._local_uncommitted_diff_lines(
            pkg_dir, status_lines if ok_status else [])
        if local_diff:
            out.append("")
            out.append("[本地未提交改动 diff]")
            out.extend(local_diff)

    def _fetch_package_remote(self, pkg_dir: Path) -> tuple[bool, str, str]:
        """拉取单个仓库远端引用（供列表刷新使用）。

        返回: (ok, err_kind, err_text)
          - err_kind: "" 成功；"not_found" 仓库不存在；"network" 网络/代理异常；
                      "no_remote" 无远端；"no_git" 非 git 仓库；"other" 其他失败
          - err_text: 原始 stderr 摘要（仅失败时有值）
        """
        repo = self._repo(pkg_dir)
        if repo is None:
            return False, "no_git", ""
        try:
            if not repo.remotes or "origin" not in [r.name for r in repo.remotes]:
                return False, "no_remote", ""
            repo.remotes.origin.fetch(prune=True)
            return True, "", ""
        except Exception as exc:
            err_text = str(exc)
            kind = self._classify_fetch_error(err_text)
            return False, kind, err_text

    @staticmethod
    def _classify_fetch_error(err: str) -> str:
        """将 git fetch 的 stderr 归类：not_found / network / reject / other。"""
        low = (err or "").lower()
        not_found_markers = [
            "repository not found",
            "does not appear to be a git repository",
            "could not read from remote repository",
            "not found",
        ]
        network_markers = [
            "ssl_error_syscall",
            "failed to connect",
            "timed out",
            "connection reset",
            "connection aborted",
            "eof",
            "proxyerror",
            "unable to access",
            "tls handshake",
        ]
        reject_markers = [
            "non-fast-forward",
            "rejected",
            "fetch first",
            "permission denied",
        ]
        if any(m in low for m in not_found_markers):
            return "not_found"
        if any(m in low for m in network_markers):
            return "network"
        if any(m in low for m in reject_markers):
            return "reject"
        return "other"

    def _prefetch_remotes_for_packages(self, package_entries: list[tuple[str, Path]]):
        """有限并发 fetch，避免刷新状态时卡死。"""
        if not package_entries:
            return
        total = len(package_entries)
        # 不再输出阶段标题，直接进入 fetch 流程
        workers = max(1, min(REMOTE_FETCH_MAX_WORKERS, total))
        done_count = 0
        missing = []     # 仓库不存在的包
        failed = []      # 其他原因失败的包
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._fetch_package_remote, pkg_dir): name
                for name, pkg_dir in package_entries
            }
            for future in concurrent.futures.as_completed(futures):
                pkg_name = futures[future]
                done_count += 1
                try:
                    ok, kind, err = future.result()
                except Exception as exc:
                    ok, kind, err = False, "other", str(exc)
                if ok:
                    line = f"  ✓ fetch [{done_count}/{total}]"
                else:
                    if kind == "not_found":
                        icon = "✗(仓库不存在)"
                        missing.append(pkg_name)
                    elif kind == "network":
                        icon = "✗(网络异常)"
                        failed.append(pkg_name)
                    elif kind == "no_remote":
                        icon = "✗(未配置 origin)"
                        missing.append(pkg_name)
                    else:
                        icon = "✗"
                        failed.append(pkg_name)
                    short_err = (err or "").splitlines(
                    )[-1][:80] if err else ""
                    line = f"  {icon} fetch [{done_count}/{total}]"
                    if short_err:
                        line += f"  ({short_err})"
                self._package_refresh_bridge.log.emit(line)
        # fetch 完成不再输出阶段标题
        if missing:
            self._package_refresh_bridge.log.emit(
                f"[提示] 以下 {len(missing)} 个包的远端仓库不存在（首次上传时程序会自动创建远端仓库）："
            )
            for n in missing:
                self._package_refresh_bridge.log.emit(f"  - {n}")
        if failed:
            self._package_refresh_bridge.log.emit(
                f"[警告] 以下 {len(failed)} 个包 fetch 失败（网络/代理异常）："
            )
            for n in failed:
                self._package_refresh_bridge.log.emit(f"  - {n}")

    def _scan_package_status(
        self, pkg_dir: Path, *, check_remote: bool = True, fetch_remote: bool = False
    ) -> tuple[bool, dict[str, int], dict[str, int], str]:
        """扫描单个包状态（给包列表统计使用，不写日志）。"""
        empty_sync: dict[str, int] = {"ahead": 0, "behind": 0}
        repo = self._repo(pkg_dir)
        if repo is None:
            return False, {}, empty_sync, "非git仓库"
        try:
            if fetch_remote:
                try:
                    repo.remotes.origin.fetch(prune=True)
                except Exception:
                    pass

            sync = dict(empty_sync)
            try:
                if check_remote and repo.head.is_detached is False and repo.active_branch.tracking_branch() is not None:
                    ahead, behind = repo.git.rev_list(
                        "--left-right", "--count", "HEAD...@{u}").split()
                    sync["ahead"] = max(0, int(ahead))
                    sync["behind"] = max(0, int(behind))
            except Exception:
                pass

            counts = {"modified": 0, "untracked": 0,
                      "conflicted": 0, "deleted": 0, "renamed": 0}
            try:
                for item in repo.index.diff(None):
                    counts["modified"] += 1
                    if item.change_type == "D":
                        counts["deleted"] += 1
                    if item.change_type == "R":
                        counts["renamed"] += 1
                counts["untracked"] = len(repo.untracked_files)
            except Exception:
                pass
            try:
                for unmerged in repo.index.unmerged_blobs().keys():
                    counts["conflicted"] += 1
            except Exception:
                pass
            return True, counts, sync, ""
        except Exception as exc:
            return False, {}, empty_sync, f"扫描失败: {exc}"

    @staticmethod
    def _format_package_status_suffix(
        counts: dict[str, int], err: str, sync: dict[str, int] | None = None
    ) -> str:
        if err:
            return f" [{err}]"
        parts: list[str] = []
        if counts.get("modified", 0):
            parts.append(f"修改{counts['modified']}")
        if counts.get("untracked", 0):
            parts.append(f"未跟踪{counts['untracked']}")
        if counts.get("deleted", 0):
            parts.append(f"删除{counts['deleted']}")
        if counts.get("renamed", 0):
            parts.append(f"重命名{counts['renamed']}")
        if counts.get("conflicted", 0):
            parts.append(f"冲突{counts['conflicted']}")
        sync = sync or {}
        if sync.get("ahead", 0) and sync.get("behind", 0):
            parts.append("分叉")
        if sync.get("behind", 0):
            parts.append(f"远端领先{sync['behind']}")
        if sync.get("ahead", 0):
            parts.append(f"本地领先{sync['ahead']}")
        if not parts:
            return " [干净]"
        return f" [{' / '.join(parts)}]"

    def _scan_all_package_status(
        self,
        package_entries: list[tuple[str, Path]],
        *,
        check_remote: bool = True,
        fetch_remote: bool = False,
    ) -> dict[str, tuple[dict[str, int], dict[str, int], str]]:
        status_map: dict[str, tuple[dict[str, int], dict[str, int], str]] = {}
        if not package_entries:
            return status_map
        total = len(package_entries)
        max_workers = max(1, min(8, (os.cpu_count() or 4), total))
        done_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for pkg_name, pkg_dir in package_entries:
                futures[executor.submit(
                    self._scan_package_status,
                    pkg_dir,
                    check_remote=check_remote,
                    fetch_remote=fetch_remote,
                )] = pkg_name
            for future in concurrent.futures.as_completed(futures):
                pkg_name = futures[future]
                done_count += 1
                self._package_refresh_bridge.log.emit(
                    f"▶ 扫描[{done_count}/{total}]")
                try:
                    ok, counts, sync, err = future.result()
                except Exception as exc:
                    ok, counts, sync, err = False, {}, {"ahead": 0, "behind": 0}, f"扫描失败: {exc}"
                status_map[pkg_name] = (
                    (counts if ok else {}),
                    sync if ok else {"ahead": 0, "behind": 0},
                    "" if ok else err,
                )
                self._package_refresh_bridge.log.emit(
                    f"已刷新[{done_count}/{total}]")
        return status_map

    def _scan_all_package_status_streaming(
        self,
        package_entries: list[tuple[str, Path]],
        *,
        check_remote: bool = True,
        fetch_remote: bool = False,
        on_entry_ready=None,
    ) -> dict[str, tuple[dict[str, int], dict[str, int], str]]:
        """并行扫描每个包，按完成顺序即时回调 UI。"""
        status_map: dict[str, tuple[dict[str, int], dict[str, int], str]] = {}
        if not package_entries:
            return status_map
        total = len(package_entries)
        max_workers = max(1, min(8, (os.cpu_count() or 4), total))
        done_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for pkg_name, pkg_dir in package_entries:
                futures[executor.submit(
                    self._scan_package_status,
                    pkg_dir,
                    check_remote=check_remote,
                    fetch_remote=fetch_remote,
                )] = pkg_name
            for future in concurrent.futures.as_completed(futures):
                pkg_name = futures[future]
                done_count += 1
                self._package_refresh_bridge.log.emit(
                    f"▶ 扫描[{done_count}/{total}]")
                try:
                    ok, counts, sync, err = future.result()
                except Exception as exc:
                    ok, counts, sync, err = False, {}, {"ahead": 0, "behind": 0}, f"扫描失败: {exc}"
                item = (
                    (counts if ok else {}),
                    sync if ok else {"ahead": 0, "behind": 0},
                    "" if ok else err,
                )
                status_map[pkg_name] = item
                if on_entry_ready:
                    try:
                        on_entry_ready(pkg_name, ok, counts, sync, err)
                    except Exception as exc:
                        self._package_refresh_bridge.log.emit(
                            f"[WARN] UI 增量更新失败 {pkg_name}: {exc}")
                self._package_refresh_bridge.log.emit(
                    f"已刷新[{done_count}/{total}]")
        return status_map

    def _style_package_status_label(
        self, label: QLabel, sync: dict[str, int], counts: dict[str, int] | None = None
    ):
        if self._is_diverged(sync):
            label.setStyleSheet(
                "font-size: 12px; color: #9333ea; font-weight: bold;")
        elif sync.get("behind", 0):
            label.setStyleSheet("font-size: 12px; color: #d97706;")
        elif sync.get("ahead", 0):
            label.setStyleSheet("font-size: 12px; color: #2563eb;")
        elif counts and any(counts.get(k, 0) for k in ("modified", "untracked", "deleted", "renamed", "conflicted")):
            # 仅本地有修改，无 ahead/behind
            label.setStyleSheet(
                "font-size: 12px; color: #16a34a; font-weight: bold;")
        else:
            label.setStyleSheet("font-size: 12px;")

    def _apply_package_status_map(
        self, status_map: dict[str, tuple[dict[str, int], dict[str, int], str]]
    ):
        for pkg_name, _pkg_dir, _remote_only in self.package_entries:
            label = self.package_status_labels.get(pkg_name)
            if label is None:
                continue
            counts, sync, err = status_map.get(
                pkg_name, ({}, {"ahead": 0, "behind": 0}, ""))
            label.setText(
                f"{pkg_name}{self._format_package_status_suffix(counts, err, sync)}")
            self._style_package_status_label(label, sync, counts)
            if not err:
                self.package_sync_map[pkg_name] = sync
            self._update_package_row_buttons(
                pkg_name, sync if not err else {}, counts if not err else {}, err)
        self._update_merge_buttons()

    def _apply_one_package_status(
        self, pkg_name: str, ok: bool, counts: object, sync: object, err: str
    ):
        label = self.package_status_labels.get(pkg_name)
        if label is None:
            return
        label.setText(
            f"{pkg_name}{self._format_package_status_suffix(counts if ok else {}, err, sync if ok else {})}"
        )
        self._style_package_status_label(
            label, sync if ok else {}, counts if ok else None)
        if ok:
            self.package_sync_map[pkg_name] = sync
        self._update_package_row_buttons(
            pkg_name, sync if ok else {}, counts if ok else {}, err)
        merge_btn = self.package_merge_buttons.get(pkg_name)
        if merge_btn is not None:
            merge_btn.setEnabled(ok and self._is_diverged(sync))
        refresh_btn = self.package_refresh_buttons.get(pkg_name)
        if refresh_btn is not None:
            refresh_btn.setEnabled(True)

    def _update_merge_buttons(self):
        for pkg_name, btn in self.package_merge_buttons.items():
            sync = self.package_sync_map.get(pkg_name, {})
            diverged = self._is_diverged(sync)
            btn.setEnabled(diverged)

    def _update_package_row_buttons(
        self,
        pkg_name: str,
        sync: dict[str, int] | None = None,
        counts: dict[str, int] | None = None,
        err: str = "",
    ):
        buttons = self.package_row_buttons.get(pkg_name) or {}
        upload_btn = buttons.get("upload")
        download_btn = buttons.get("download")
        delete_local_btn = buttons.get("delete_local")
        delete_remote_btn = buttons.get("delete_remote")
        web_btn = buttons.get("web")
        busy = self._package_refresh_running
        remote_only = any(name == pkg_name and remote_only for name,
                          _, remote_only in self.package_entries)
        local_exists = False
        for name, path, _remote_only in self.package_entries:
            if name == pkg_name:
                local_exists = path.exists()
                break
        diverged = self._is_diverged(sync or {})
        has_local_changes = bool(counts and any(counts.get(k, 0) for k in (
            "modified", "untracked", "deleted", "renamed", "conflicted")))
        can_upload = (
            not remote_only) and local_exists and not busy and not bool(err)
        can_download = (not busy) and (local_exists or remote_only)
        can_delete_local = (not busy) and local_exists and not remote_only
        can_delete_remote = (not busy) and not remote_only
        can_web = not busy
        if upload_btn is not None:
            upload_btn.setEnabled(can_upload and (
                diverged or has_local_changes or bool(counts)))
        if download_btn is not None:
            download_btn.setEnabled(can_download)
        if delete_local_btn is not None:
            delete_local_btn.setEnabled(can_delete_local)
        if delete_remote_btn is not None:
            delete_remote_btn.setEnabled(can_delete_remote)
        if web_btn is not None:
            web_btn.setEnabled(can_web)

    def _update_one_package_status(self, pkg_name: str, pkg_dir: Path):
        """后台线程刷新单包状态，完成后通过信号回调更新 UI。"""
        refresh_btn = self.package_refresh_buttons.get(pkg_name)
        if refresh_btn is not None:
            refresh_btn.setEnabled(False)

        def _worker():
            ok, counts, sync, err = self._scan_package_status(
                pkg_dir, check_remote=True, fetch_remote=False
            )
            self._package_refresh_bridge.one_ready.emit(
                pkg_name, ok, counts, sync, err
            )

        self._start_worker(_worker)

    def _update_one_package_status_ui(
        self, pkg_name: str, ok: bool, counts: object, sync: object, err: str
    ):
        self._apply_one_package_status(pkg_name, ok, counts, sync, err)

    def _on_one_package_status_ready(
        self, pkg_name: str, ok: bool, counts: object, sync: object, err: str
    ):
        """单包状态后台扫描完成的主线程回调。"""
        if pkg_name == "__package_list__":
            return
        scroll = self._list_scroll_pos()
        self._apply_one_package_status(pkg_name, ok, counts, sync, err)
        self._restore_list_scroll(scroll)

    def _open_package_in_browser(self, pkg_name: str):
        """用默认浏览器打开包对应的 GitHub 仓库页面。"""
        owner = (self.owner_edit.text().strip()
                 if self.owner_edit else "") or "Lugwit123"
        url = f"https://github.com/{owner}/{pkg_name}"
        self._log(f"[info] 在浏览器中打开: {url}")
        try:
            webbrowser.open(url)
        except Exception as exc:
            self._log(f"[ERR] 打开浏览器失败: {exc}")
            QMessageBox.warning(self, "打开浏览器失败", str(exc))

    def refresh_package_status(self):
        if not self.package_entries:
            self._start_package_list_refresh(scan_status=True)
            return
        if self._package_refresh_running:
            return
        scannable = [
            (name, path) for name, path, remote_only in self.package_entries if not remote_only
        ]
        if not scannable:
            return
        self._package_refresh_token += 1
        token = self._package_refresh_token
        self._last_list_scan_status = True
        self._set_package_refresh_busy(True)
        self._log_phase_start("刷新包状态")

        def _worker():
            try:
                status_map = self._scan_all_package_status_streaming(
                    scannable,
                    check_remote=True,
                    fetch_remote=False,
                    on_entry_ready=lambda n, ok, counts, sync, err: self._package_refresh_bridge.one_ready.emit(
                        n, ok, counts, sync, err
                    ),
                )
                self._package_refresh_bridge.status_ready.emit(
                    token, status_map)
            except Exception as exc:
                self._package_refresh_bridge.failed.emit(token, str(exc))

        self._start_worker(_worker)

    def refresh_packages(self):
        self._start_package_list_refresh(scan_status=True)

    def _start_package_list_refresh(self, *, scan_status: bool = True):
        if self._package_refresh_running:
            return
        self._package_refresh_token += 1
        token = self._package_refresh_token
        self._set_package_refresh_busy(True)
        local_entries = [(name, path, False)
                         for name, path in self._local_package_entries()]
        self._render_package_list(local_entries, {}, loading=True)
        owner = self.owner_edit.text().strip() or "Lugwit123"
        self._log_phase_start("加载包列表")

        def _worker():
            try:
                local = self._local_package_entries()
                local_names = {name for name, _ in local}
                entries: list[tuple[str, Path, bool]] = [
                    (name, path, False) for name, path in local]

                if scan_status:
                    self._package_refresh_bridge.log.emit("获取远端仓库列表…")
                    ok, remote_names, err = self._fetch_remote_repo_names_quiet(
                        owner)
                    if ok:
                        self._package_refresh_bridge.log.emit(
                            f"远端仓库数: {len(remote_names)}")
                        for name in sorted(set(remote_names), key=str.lower):
                            if name in local_names or name in SKIP_DIRS:
                                continue
                            entries.append(
                                (name, self._remote_package_path(name), True))
                    elif self._github_token():
                        self._package_refresh_bridge.log.emit(
                            f"[WARN] 获取远端仓库列表失败，仅显示本地包: {err}")

                self._package_refresh_bridge.list_ready.emit(
                    token, entries, {}, [], scan_status)

                if not scan_status:
                    self._package_refresh_bridge.status_ready.emit(token, {})
                    return

                scannable = [(name, path) for name, path,
                             remote_only in entries if not remote_only]
                if scannable:
                    self._prefetch_remotes_for_packages(scannable)
                    status_map = self._scan_all_package_status_streaming(
                        scannable,
                        check_remote=True,
                        fetch_remote=False,
                        on_entry_ready=lambda n, ok, counts, sync, err: self._package_refresh_bridge.one_ready.emit(
                            n, ok, counts, sync, err
                        ),
                    )
                else:
                    status_map = {}
                self._package_refresh_bridge.status_ready.emit(
                    token, status_map)
            except Exception as exc:
                self._package_refresh_bridge.failed.emit(token, str(exc))

        self._start_worker(_worker)

    def _on_package_list_ready(
        self,
        token: int,
        package_entries: list,
        status_map: dict,
        warnings: list,
        scan_status: bool = True,
    ):
        if token != self._package_refresh_token:
            return
        self._last_list_scan_status = scan_status
        self._render_package_list(
            package_entries, status_map, loading=scan_status)
        for line in warnings:
            self._log(line)
        local_count = sum(
            1 for _, _, remote_only in package_entries if not remote_only)
        remote_count = len(package_entries) - local_count
        if scan_status:
            if remote_count:
                self._log(
                    f"已渲染 {local_count} 个本地包、{remote_count} 个仅远端仓库，开始扫描。")
            else:
                self._log(f"已渲染 {local_count} 个包，开始扫描。")
            self._set_package_refresh_busy(True)
        elif remote_count:
            self._log(f"已渲染 {local_count} 个本地包、{remote_count} 个仅远端仓库。")
        else:
            self._log(f"已渲染 {local_count} 个包。")

    def _on_package_status_ready(self, token: int, status_map: dict):
        if token != self._package_refresh_token:
            return
        scroll = self._list_scroll_pos()
        self._apply_package_status_map(status_map)
        self._restore_list_scroll(scroll)
        if self._last_list_scan_status:
            self._log("已全部刷新")
            self._log_phase_end("刷新包状态")
        self._log_phase_end("加载包列表")
        self._set_package_refresh_busy(False)
        self._update_merge_buttons()
        self._restore_list_scroll(scroll)

    def _on_package_refresh_failed(self, token: int, message: str):
        if token != self._package_refresh_token:
            return
        self._log(f"[ERR] 包列表刷新失败: {message}")
        self._log_phase_end("加载包列表", ok=False)
        self._log_phase_end("刷新包状态", ok=False)
        self._set_package_refresh_busy(False)

    def _wuwo_bat_path(self) -> Path | None:
        wuwo_bat = self.rez_source.parent / "wuwo" / "wuwo.bat"
        return wuwo_bat if wuwo_bat.is_file() else None

    def _read_package_aliases(self, pkg_name: str, pkg_dir: Path) -> list[tuple[str, str]]:
        if pkg_name in self._package_alias_cache:
            aliases = self._package_alias_cache[pkg_name]
            self._log(f"[alias] {pkg_name}: 使用缓存，别名数={len(aliases)}")
            return aliases
        aliases: list[tuple[str, str]] = []
        seen: set[str] = set()
        if pkg_dir.is_dir():
            if pkg_name == "wuwo":
                # wuwo 目录里包含 Python/Rez 运行环境，不能全量 rglob，否则会扫到 site-packages 里的测试包。
                search_roots = [pkg_dir / "packages"]
            else:
                # rez-package-source/<pkg>/<version>/package.py，只扫描直接版本目录。
                search_roots = [pkg_dir]
            package_files: list[Path] = []
            for search_root in search_roots:
                if not search_root.is_dir():
                    continue
                package_files.extend(search_root.glob("*/package.py"))
            package_files = sorted(set(package_files), reverse=True)
            roots_text = ", ".join(str(p) for p in search_roots)
            self._log(
                f"[alias] {pkg_name}: 扫描 package.py 数={len(package_files)} roots={roots_text}")
            for package_py in package_files:
                try:
                    text = package_py.read_text(
                        encoding="utf-8", errors="ignore")
                except OSError as exc:
                    self._log(
                        f"[alias][WARN] {pkg_name}: 读取失败 {package_py}: {exc}")
                    continue
                parsed_aliases = parse_rez_package_aliases(text)
                rel_path = package_py
                try:
                    rel_path = package_py.relative_to(pkg_dir)
                except ValueError:
                    pass
                self._log(
                    f"[alias] {pkg_name}: {rel_path} 解析到 {len(parsed_aliases)} 个 alias")
                for alias_name, alias_command in parsed_aliases:
                    if alias_name not in seen:
                        aliases.append((alias_name, alias_command))
                        seen.add(alias_name)
                        self._log(
                            f"[alias] {pkg_name}: {alias_name} => {alias_command}")
                    else:
                        self._log(f"[alias] {pkg_name}: 跳过重复别名 {alias_name}")
        else:
            self._log(f"[alias][WARN] {pkg_name}: 包目录不存在 {pkg_dir}")
        self._package_alias_cache[pkg_name] = aliases
        self._log(f"[alias] {pkg_name}: 最终别名数={len(aliases)}")
        return aliases

    def _format_rez_launch_cmd(self, pkg_name: str, alias_name: str) -> str:
        return format_rez_alias_launch_cmd(
            pkg_name,
            alias_name,
            wuwo_bat=self._wuwo_bat_path(),
        )

    def _copy_package_launch_cmd(self, pkg_name: str, alias_name: str, alias_command: str):
        cmd = self._format_rez_launch_cmd(pkg_name, alias_name)
        copy_text = f"{cmd}  # {{{alias_command}}}"
        QApplication.clipboard().setText(copy_text)
        self._log(f"[copy] {copy_text}")

    def _build_clean_env(self) -> dict:
        """清理 rez 脏环境变量，避免传递给子进程时污染。"""
        dirty_keys = [
            "PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE",
            "__PYVENV_LAUNCHER__",
            "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_EXE",
            "CONDA_PYTHON_EXE", "CONDA_SHLVL", "_CE_CONDA", "_CE_M",
            "REZ_USED", "REZ_USED_REQUEST", "REZ_USED_RESOLVE",
            "REZ_RXT_FILE", "REZ_ENV_PROMPT",
            "REZ_CONFIG_FILE", "REZ_PACKAGES_PATH",
        ]
        env = os.environ.copy()
        for key in dirty_keys:
            env.pop(key, None)
        return env

    def _launch_package_alias(self, pkg_name: str, alias_name: str, alias_command: str):
        wuwo_bat = self._wuwo_bat_path()
        if wuwo_bat is None:
            QMessageBox.warning(self, "启动失败", "未找到 wuwo.bat，无法启动。")
            return
        cmd_text = self._format_rez_launch_cmd(pkg_name, alias_name)
        self._log(f"[launch] {cmd_text}  # {{{alias_command}}}")
        clean_env = self._build_clean_env()

        # 读取 run_buttonGroup 选择：none / cmd / ps
        run_mode = "none"
        if self.run_buttonGroup:
            checked = self.run_buttonGroup.checkedButton()
            if checked:
                run_mode = checked.text().lower()
        self._log(f"[run_mode] {run_mode}")

        try:
            # wuwor.bat 是同目录下的转发器，和托盘 Tray.py 完全一致的启动方式
            wuwo_dir = str(wuwo_bat.parent)
            wuwor_bat = os.path.join(wuwo_dir, "wuwor.bat")
            solo = " .solo" if (
                self.solo_checkBox and self.solo_checkBox.isChecked()) else ""
            if sys.platform == "win32":
                if run_mode == "none":
                    # 当前进程运行，不打开新窗口
                    final_cmd = f'"{wuwor_bat}" {pkg_name}{solo} -- {alias_name}'
                elif run_mode == "ps":
                    # 新 PowerShell 窗口
                    final_cmd = f'cmd.exe /c start "{pkg_name}" "{wuwor_bat}" {pkg_name} .ps{solo} -- {alias_name}'
                else:
                    # cmd: 新 cmd 窗口
                    final_cmd = f'cmd.exe /c "{wuwor_bat}" {pkg_name} .cmd{solo} -- {alias_name}'
                self._log(f"[exec] {final_cmd}")
                self._log(f"[启动包时间] {datetime.datetime.now()}")
                debug = self.debug_start_pkg.isChecked() if self.debug_start_pkg else False
                if debug:
                    # debug 模式：当前进程运行，不创建新控制台
                    subprocess.Popen(
                        final_cmd,
                        shell=True,
                        cwd=wuwo_dir,
                        env=clean_env,
                    )
                else:
                    # 正常模式：创建新控制台窗口
                    subprocess.Popen(
                        final_cmd,
                        shell=True,
                        cwd=wuwo_dir,
                        env=clean_env,
                        creationflags=getattr(
                            subprocess, "CREATE_NEW_CONSOLE", 0x00000010),
                    )

                # 始终从 wuwo_rez.log 读取日志显示到面板
                import threading
                log_dir = clean_env.get("LOG_DIR", r"D:\Temp\Log\rez_pkg_log")
                log_file = os.path.join(log_dir, "wuwo_rez.log")

                def _read_wuwo_log(lf, name):
                    # 等待日志文件生成（wuwo_rez.py 重写模式）
                    for _ in range(20):
                        if os.path.isfile(lf):
                            break
                        import time as _t
                        _t.sleep(0.2)
                    else:
                        self._log(f"[{name}] log file not found: {lf}")
                        return
                    # 读取并显示
                    prev = ""
                    for _ in range(60):  # 最多读 12s
                        try:
                            content = open(lf, "r", encoding="utf-8").read()
                            if content != prev:
                                new_lines = content[len(prev):]
                                for line in new_lines.strip().splitlines():
                                    if line:
                                        self._log(f"[{name}] {line}")
                                prev = content
                        except Exception:
                            pass
                        import time as _t
                        _t.sleep(0.2)
                    # 最终再读一次确保完整
                    try:
                        content = open(lf, "r", encoding="utf-8").read()
                        if content != prev:
                            for line in content[len(prev):].strip().splitlines():
                                if line:
                                    self._log(f"[{name}] {line}")
                    except Exception:
                        pass

                self._start_worker(
                    lambda: _read_wuwo_log(log_file, pkg_name))
            else:
                inner = f"wuwor {pkg_name} .ps{solo} -- {alias_name}"
                final_args = ["bash", "-lc", inner]
                self._log(f"[exec] {' '.join(final_args)}")
                subprocess.Popen(final_args, cwd=wuwo_dir, env=clean_env)
        except Exception as exc:
            QMessageBox.warning(self, "启动失败", str(exc))

    def _show_package_row_context_menu(
        self,
        pkg_name: str,
        pkg_dir: Path,
        remote_only: bool,
        global_pos,
    ):
        menu = QMenu(self)
        if remote_only or not pkg_dir.is_dir():
            action = menu.addAction("（仅远端，本地无 package.py）")
            action.setEnabled(False)
            menu.exec(global_pos)
            return

        aliases = self._read_package_aliases(pkg_name, pkg_dir)
        self._log(f"[alias-menu] {pkg_name}: 打开右键菜单，别名数={len(aliases)}")
        if not aliases:
            action = menu.addAction("（未定义命令别名）")
            action.setEnabled(False)
            menu.exec(global_pos)
            return

        for alias_name, alias_command in aliases:
            launch_cmd = self._format_rez_launch_cmd(pkg_name, alias_name)
            menu_text = f"{launch_cmd}  # {{{alias_command}}}"
            launch_action = QAction(f"启动: {menu_text}", self)
            launch_action.setToolTip(menu_text)
            launch_action.triggered.connect(
                lambda _=False, p=pkg_name, a=alias_name, c=alias_command: self._launch_package_alias(
                    p, a, c)
            )
            menu.addAction(launch_action)

            copy_action = QAction(f"复制: {menu_text}", self)
            copy_action.setToolTip(menu_text)
            copy_action.triggered.connect(
                lambda _=False, p=pkg_name, a=alias_name, c=alias_command: self._copy_package_launch_cmd(
                    p, a, c)
            )
            menu.addAction(copy_action)

        menu.exec(global_pos)

    def _render_package_list(
        self,
        package_entries: list[tuple[str, Path, bool]],
        status_map: dict,
        *,
        loading: bool = False,
    ):
        """增量刷新包列表：复用已有行控件，仅删除/新增变化的包。

        避免全量 deleteLater() 产生 DeferredDelete 事件洪水，
        防止 processEvents() 期间 C++ 层访问已删除控件导致 abort()。
        """
        if not hasattr(self, '_package_rows'):
            self._package_rows: dict[str, QWidget] = {}

        new_names = {name for name, _, _ in package_entries}

        # ---- 1. 删除不再存在的行 ----
        for name in list(self._package_rows.keys()):
            if name not in new_names:
                self._package_rows.pop(name).deleteLater()

        # ---- 2. 清空 layout，但保留复用行 ----
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                # 仅保留 _package_rows 中已有的行控件，其余（分隔线、节标题）全部删除
                is_known_row = any(
                    w is row for row in self._package_rows.values())
                if not is_known_row:
                    w.deleteLater()

        # ---- 3. 保存旧跟踪引用，准备新结构 ----
        old_labels = dict(self.package_status_labels)
        old_buttons = dict(self.package_row_buttons)
        old_merge: dict[str, QPushButton] = dict(self.package_merge_buttons)
        old_checkboxes: dict[str, QCheckBox] = dict(self.package_checkboxes)

        self.row_buttons = []
        self.package_row_buttons = {}
        self.package_status_labels = {}
        self.package_sync_map = {}
        self.package_merge_buttons = {}
        self.package_refresh_buttons = {}
        self.package_checkboxes = {}
        self.package_entries = package_entries

        remote_section_started = False

        for pkg_name, pkg_dir, remote_only in package_entries:
            # --- 远端分隔线 ---
            if remote_only and not remote_section_started:
                remote_section_started = True
                section = QLabel("── 仅远端（本地无） ──")
                section.setStyleSheet(
                    "font-size: 12px; color: #9aa0a6; padding: 4px 0px;")
                self.list_layout.addWidget(section)
                sep_hdr = QFrame()
                sep_hdr.setFrameShape(QFrame.HLine)
                sep_hdr.setFrameShadow(QFrame.Plain)
                sep_hdr.setFixedHeight(2)
                sep_hdr.setStyleSheet(
                    "color: #5a5a62; margin: 0px; padding: 0px;")
                self.list_layout.addWidget(sep_hdr)

            # --- 复用或新建行 ---
            reusing = pkg_name in self._package_rows

            if reusing:
                # ★ 复用已有行：不删除、不重建，仅更新 label 文本
                row = self._package_rows[pkg_name]
                label = old_labels.get(pkg_name)
                buttons = old_buttons.get(pkg_name, {})
                merge_btn = old_merge.get(pkg_name)
                chk = old_checkboxes.get(pkg_name)
            else:
                # ★ 创建新行
                row = QWidget()
                self._package_rows[pkg_name] = row

                def _show_pkg_context_menu(
                    pos, *, n=pkg_name, d=pkg_dir, ro=remote_only, w=row
                ):
                    self._show_package_row_context_menu(
                        n, d, ro, w.mapToGlobal(pos))

                row.setContextMenuPolicy(Qt.CustomContextMenu)
                row.customContextMenuRequested.connect(_show_pkg_context_menu)
                row_lay = QHBoxLayout(row)
                row_lay.setContentsMargins(1, 0, 1, 0)
                row_lay.setSpacing(4)

                # 行首勾选框：供顶部「上传选中」批量上传（仅本地可上传的包）
                if remote_only:
                    chk = None
                else:
                    chk = QCheckBox()
                    chk.setFocusPolicy(Qt.NoFocus)
                    chk.setMaximumWidth(22)
                    chk.setToolTip("勾选后点顶部「上传选中」批量上传")
                    row_lay.addWidget(chk)

                label = QLabel()
                label.setMinimumWidth(200)
                label.setContextMenuPolicy(Qt.CustomContextMenu)
                label.customContextMenuRequested.connect(
                    lambda pos, w=label, fn=_show_pkg_context_menu: fn(
                        pos, w=w)
                )
                row_lay.addWidget(label, 1)

                row_busy = loading and self._package_refresh_running

                # 上传
                btn_up = QPushButton("上传")
                btn_up.setFocusPolicy(Qt.NoFocus)
                btn_up.setProperty("class", "row-btn")
                btn_up.setMinimumHeight(20)
                btn_up.setMaximumWidth(58)
                if remote_only:
                    btn_up.setEnabled(False)
                    btn_up.setToolTip("本地尚无此仓库，请先下载")
                else:
                    btn_up.clicked.connect(
                        lambda _=False, p=pkg_dir: self.upload_one(p)
                    )
                if row_busy:
                    btn_up.setEnabled(False)
                btn_up.setContextMenuPolicy(Qt.CustomContextMenu)
                btn_up.customContextMenuRequested.connect(
                    lambda pos, w=btn_up, fn=_show_pkg_context_menu: fn(
                        pos, w=w)
                )
                row_lay.addWidget(btn_up)
                self.row_buttons.append(btn_up)

                # 下载
                btn_down = QPushButton("下载")
                btn_down.setFocusPolicy(Qt.NoFocus)
                btn_down.setProperty("class", "row-btn")
                btn_down.setMinimumHeight(20)
                btn_down.setMaximumWidth(58)
                btn_down.clicked.connect(
                    lambda _=False, p=pkg_dir: self.download_one(p)
                )
                if row_busy:
                    btn_down.setEnabled(False)
                btn_down.setContextMenuPolicy(Qt.CustomContextMenu)
                btn_down.customContextMenuRequested.connect(
                    lambda pos, w=btn_down, fn=_show_pkg_context_menu: fn(
                        pos, w=w)
                )
                row_lay.addWidget(btn_down)
                self.row_buttons.append(btn_down)

                # 删本地
                local_exists = pkg_dir.exists()
                btn_del_local = QPushButton("删本地")
                btn_del_local.setFocusPolicy(Qt.NoFocus)
                btn_del_local.setProperty("class", "row-btn")
                btn_del_local.setMinimumHeight(20)
                btn_del_local.setMaximumWidth(58)
                if remote_only or not local_exists:
                    btn_del_local.setEnabled(False)
                    btn_del_local.setToolTip("本地目录不存在")
                else:
                    btn_del_local.setToolTip("永久删除本地目录")
                    btn_del_local.clicked.connect(
                        lambda _=False, p=pkg_dir: self.delete_local_one(p)
                    )
                if row_busy:
                    btn_del_local.setEnabled(False)
                btn_del_local.setContextMenuPolicy(Qt.CustomContextMenu)
                btn_del_local.customContextMenuRequested.connect(
                    lambda pos, w=btn_del_local, fn=_show_pkg_context_menu: fn(
                        pos, w=w)
                )
                row_lay.addWidget(btn_del_local)
                self.row_buttons.append(btn_del_local)

                # 删远端
                btn_del_remote = QPushButton("删远端")
                btn_del_remote.setFocusPolicy(Qt.NoFocus)
                btn_del_remote.setProperty("class", "row-btn")
                btn_del_remote.setMinimumHeight(20)
                btn_del_remote.setMaximumWidth(58)
                btn_del_remote.setToolTip("永久删除 GitHub 仓库")
                btn_del_remote.clicked.connect(
                    lambda _=False, p=pkg_dir: self.delete_remote_one(p)
                )
                if row_busy:
                    btn_del_remote.setEnabled(False)
                btn_del_remote.setContextMenuPolicy(Qt.CustomContextMenu)
                btn_del_remote.customContextMenuRequested.connect(
                    lambda pos, w=btn_del_remote, fn=_show_pkg_context_menu: fn(
                        pos, w=w
                    )
                )
                row_lay.addWidget(btn_del_remote)
                self.row_buttons.append(btn_del_remote)

                # 刷新
                btn_refresh = QPushButton("刷新")
                btn_refresh.setFocusPolicy(Qt.NoFocus)
                btn_refresh.setProperty("class", "refresh-btn")
                btn_refresh.setMinimumHeight(20)
                btn_refresh.setMaximumWidth(46)
                btn_refresh.setToolTip("刷新此包的 git 状态")
                if remote_only:
                    btn_refresh.setEnabled(False)
                else:
                    btn_refresh.clicked.connect(
                        lambda _=False, n=pkg_name, p=pkg_dir: (
                            self._update_one_package_status(n, p)
                        )
                    )
                if row_busy:
                    btn_refresh.setEnabled(False)
                btn_refresh.setContextMenuPolicy(Qt.CustomContextMenu)
                btn_refresh.customContextMenuRequested.connect(
                    lambda pos, w=btn_refresh, fn=_show_pkg_context_menu: fn(
                        pos, w=w
                    )
                )
                row_lay.addWidget(btn_refresh)
                self.row_buttons.append(btn_refresh)
                if not remote_only:
                    self.package_refresh_buttons[pkg_name] = btn_refresh

                # 网页
                btn_web = QPushButton("网页")
                btn_web.setFocusPolicy(Qt.NoFocus)
                btn_web.setProperty("class", "row-btn")
                btn_web.setMinimumHeight(20)
                btn_web.setMaximumWidth(46)
                owner = (
                    self.owner_edit.text().strip() if self.owner_edit else ""
                ) or "Lugwit123"
                btn_web.setToolTip(
                    f"在浏览器中打开 https://github.com/{owner}/{pkg_name}"
                )
                btn_web.clicked.connect(
                    lambda _=False, n=pkg_name: self._open_package_in_browser(
                        n)
                )
                btn_web.setContextMenuPolicy(Qt.CustomContextMenu)
                btn_web.customContextMenuRequested.connect(
                    lambda pos, w=btn_web, fn=_show_pkg_context_menu: fn(
                        pos, w=w)
                )
                row_lay.addWidget(btn_web)
                self.row_buttons.append(btn_web)

                buttons = {
                    "upload": btn_up,
                    "download": btn_down,
                    "delete_local": btn_del_local,
                    "delete_remote": btn_del_remote,
                    "refresh": btn_refresh,
                    "web": btn_web,
                }
                merge_btn = None

            # --- 更新 label 文字和样式（新建与复用都需要）---
            if remote_only:
                label.setText(f"{pkg_name} [仅远端]")
                label.setStyleSheet("font-size: 12px;")
            elif loading and pkg_name not in status_map:
                label.setText(f"{pkg_name} [扫描中…]")
                label.setStyleSheet("font-size: 12px; color: #9aa0a6;")
            else:
                counts, sync, err = status_map.get(
                    pkg_name, ({}, {"ahead": 0, "behind": 0}, "")
                )
                status_text = self._format_package_status_suffix(
                    counts, err, sync)
                label.setText(f"{pkg_name}{status_text}")
                self._style_package_status_label(label, sync, counts)
                if not err:
                    self.package_sync_map[pkg_name] = sync

            # --- 收集复用行的按钮到新的跟踪结构 ---
            if reusing:
                btns = old_buttons.get(pkg_name, {})
                for key, btn in btns.items():
                    if btn not in self.row_buttons:
                        self.row_buttons.append(btn)
                if not remote_only and "refresh" in btns:
                    self.package_refresh_buttons[pkg_name] = btns["refresh"]
                buttons = btns

            # --- 记录跟踪结构 ---
            self.package_status_labels[pkg_name] = label
            self.package_row_buttons[pkg_name] = buttons
            if merge_btn is not None:
                self.package_merge_buttons[pkg_name] = merge_btn
            if chk is not None:
                self.package_checkboxes[pkg_name] = chk

            # --- 添加到 layout（分隔线每次重建）---
            self.list_layout.addWidget(row)
            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setFrameShadow(QFrame.Plain)
            sep.setFixedHeight(2)
            self.list_layout.addWidget(sep)

    def _ensure_git_repo(self, pkg_dir: Path) -> bool:
        if (pkg_dir / ".git").is_dir():
            return True
        if self._repo(pkg_dir) is not None:
            return True
        ok, err = git_helper.init_repo(pkg_dir)
        if not ok:
            self._log(f"[ERR] {pkg_dir.name} git init 失败: {err}")
            return False
        return True

    def _ensure_gitignore(self, pkg_dir: Path):
        path = pkg_dir / ".gitignore"
        existing = set()
        if path.exists():
            existing = {
                line.strip()
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.strip()
            }
        lines = list(existing)
        changed = False
        extra = PACKAGE_EXTRA_IGNORE.get(pkg_dir.name, [])
        for line in [*IGNORE_LINES, *extra]:
            if line not in existing:
                lines.append(line)
                changed = True
        if changed:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _find_oversized_push_blobs(
        self, pkg_dir: Path, upstream_ref: str | None = None
    ) -> list[tuple[str, int]]:
        """列出即将 push 的提交中超过 GitHub 限制（100MB）的 blob。"""
        repo = self._repo(pkg_dir)
        if repo is None:
            return []
        try:
            rev_range = f"{upstream_ref}..HEAD" if upstream_ref else "HEAD"
            rev_list = repo.git.rev_list("--objects", rev_range)
            if not (rev_list or "").strip():
                return []
            cat_ok, cat_out = git_helper.cat_file_batch_check(
                pkg_dir, rev_list)
            if not cat_ok:
                raise RuntimeError(cat_out)
        except Exception as exc:
            self._log(f"[WARN] 扫描大文件失败: {exc}")
            return []
        oversized: list[tuple[str, int]] = []
        for line in (cat_out or "").splitlines():
            parts = line.split(maxsplit=3)
            if len(parts) < 4 or parts[0] != "blob":
                continue
            try:
                size = int(parts[2])
            except ValueError:
                continue
            if size >= GITHUB_FILE_LIMIT_BYTES:
                oversized.append((parts[3], size))
        oversized.sort(key=lambda x: x[1], reverse=True)
        return oversized

    @staticmethod
    def _format_bytes(num: int) -> str:
        if num >= 1024 * 1024:
            return f"{num / (1024 * 1024):.2f} MB"
        if num >= 1024:
            return f"{num / 1024:.1f} KB"
        return f"{num} B"

    def _confirm_push_with_oversized_files(
        self, pkg_name: str, oversized: list[tuple[str, int]]
    ) -> bool:
        lines = [
            f"{path} ({self._format_bytes(size)})"
            for path, size in oversized[:20]
        ]
        if len(oversized) > 20:
            lines.append(f"...(共 {len(oversized)} 个超限文件)")
        body = (
            f"{pkg_name} 即将 push 的提交中含有超过 GitHub 100MB 硬限制的文件，"
            "推送必然失败。\n\n"
            "请将这些路径加入 .gitignore，并用 git rm --cached 从版本库移除后重新提交。\n\n"
            + "\n".join(lines)
        )
        QMessageBox.critical(self, "无法推送：文件过大", body)
        return False

    def _ensure_remote(self, pkg_dir: Path, pkg_name: str, owner: str):
        remote_url = f"https://github.com/{owner}/{pkg_name}.git"
        ok, _ = self._run(["git", "remote", "get-url", "origin"], cwd=pkg_dir)
        if ok:
            self._run(["git", "remote", "set-url",
                      "origin", remote_url], cwd=pkg_dir)
        else:
            self._run(["git", "remote", "add", "origin",
                      remote_url], cwd=pkg_dir)

    def _current_branch(self, pkg_dir: Path) -> str:
        ok, msg = self._run(["git", "branch", "--show-current"], cwd=pkg_dir)
        branch = (msg or "").strip().splitlines(
        )[-1] if ok and msg.strip() else "main"
        return branch or "main"

    def _has_git_remote(self, pkg_dir: Path, name: str = "origin") -> bool:
        ok, _ = self._run(["git", "remote", "get-url", name], cwd=pkg_dir)
        return ok

    def _try_create_github_repo_and_push(
        self, pkg_dir: Path, pkg_name: str, owner: str, branch: str
    ) -> bool:
        """远端仓库不存在时：GitHub API 建仓后 push（兼容本地已有 origin）。"""
        ok_create, msg_create = github_helper.create_repo(
            owner, pkg_name, self._github_token(), public=True
        )
        if not ok_create:
            low = (msg_create or "").lower()
            if "already exists" not in low and "name already exists" not in low:
                self._log(f"[ERR] {pkg_name} 创建远端仓库失败: {msg_create}")
                QMessageBox.warning(
                    self,
                    "上传失败",
                    f"{pkg_name} 创建远端仓库失败:\n{msg_create}",
                )
                return False
            self._log(f"[warn] {pkg_name} 远端仓库已存在，继续 push")

        if not self._has_git_remote(pkg_dir):
            remote_url = f"https://github.com/{owner}/{pkg_name}.git"
            self._run(["git", "remote", "add", "origin",
                      remote_url], cwd=pkg_dir)

        ok_push, msg_push = self._run_blocking_with_events(
            git_helper.push,
            pkg_dir,
            branch,
            set_upstream=True,
        )
        if ok_push:
            self._log(f"[ok] {pkg_name} created and pushed")
            self._log_upload_summary(pkg_dir, pkg_name)
            return True

        self._log(f"[ERR] {pkg_name} 建仓后 push 失败: {msg_push}")
        QMessageBox.warning(
            self,
            "上传失败",
            f"{pkg_name} 远端仓库已创建，但 push 失败:\n{msg_push}",
        )
        return False

    @staticmethod
    def _should_try_create_repo(push_err: str) -> bool:
        """仅在明显“远端仓库不存在”时才尝试自动创建远端仓库。"""
        low = (push_err or "").lower()
        repo_not_found_markers = [
            "repository not found",
            "remote: repository not found",
            "not found",
            "does not appear to be a git repository",
            "could not read from remote repository",
        ]
        network_markers = [
            "ssl_error_syscall",
            "failed to connect",
            "timed out",
            "connection reset",
            "connection aborted",
            "eof",
            "proxyerror",
            "unable to access",
        ]
        reject_markers = [
            "non-fast-forward",
            "rejected",
            "fetch first",
            "tip of your current branch is behind",
            "protected branch",
            "permission denied",
        ]
        tooling_markers = [
            "kill_after_timeout",
            "not supported on windows",
        ]
        if any(m in low for m in tooling_markers + network_markers + reject_markers):
            return False
        return any(m in low for m in repo_not_found_markers)

    @staticmethod
    def _is_git_tooling_error(err: str) -> bool:
        low = (err or "").lower()
        return "kill_after_timeout" in low or "not supported on windows" in low

    @staticmethod
    def _is_non_fast_forward_error(push_err: str) -> bool:
        low = (push_err or "").lower()
        markers = [
            "non-fast-forward",
            "tip of your current branch is behind",
            "fetch first",
            "rejected",
        ]
        return any(m in low for m in markers)

    def _ask_force_push(self, pkg_name: str, branch: str, reason: str) -> bool:
        """push 失败时询问是否强制覆盖远端。"""
        confirm = QMessageBox.question(
            self,
            "上传冲突",
            (
                f"{pkg_name} 推送失败：\n{reason}\n\n"
                f"是否强制用本地覆盖远端？\n"
                f"git push -u origin {branch} --force-with-lease\n\n"
                "这会丢弃远端同分支上未合并的提交，请谨慎确认。"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return confirm == QMessageBox.Yes

    def _ask_force_download(self, pkg_name: str, reason: str) -> bool:
        """pull 失败时询问是否强制覆盖本地。"""
        confirm = QMessageBox.question(
            self,
            "下载冲突",
            (
                f"{pkg_name} 下载失败：\n{reason}\n\n"
                "是否强制用远端覆盖本地？将执行：\n"
                "1) git fetch --all --prune\n"
                "2) git reset --hard <upstream>\n"
                "3) git clean -fd\n\n"
                "这会丢弃本地未提交改动和未跟踪文件，请谨慎确认。"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return confirm == QMessageBox.Yes

    def _force_download_workspace(self, pkg_dir: Path, pkg_name: str, upstream_ref: str | None = None) -> bool:
        """fetch + reset --hard + clean -fd 强制同步远端到本地。"""
        repo = self._repo(pkg_dir)
        if repo is None:
            self._log(f"[ERR] {pkg_name} 不是 git 仓库")
            QMessageBox.warning(self, "下载失败", f"{pkg_name} 不是 git 仓库。")
            return False
        try:
            if repo.remotes and any(r.name == "origin" for r in repo.remotes):
                repo.remotes.origin.fetch(prune=True)
            else:
                self._log(f"[warn] {pkg_name} 未找到 origin，跳过 fetch")

            target_ref = upstream_ref or self._upstream_ref(pkg_dir)
            if not target_ref:
                try:
                    target_ref = f"origin/{repo.active_branch.name}"
                except Exception:
                    target_ref = "origin/main"
                self._log(f"[warn] {pkg_name} 无上游分支，回退使用 {target_ref}")

            repo.git.reset("--hard", target_ref)
            repo.git.clean("-fd")
        except Exception as exc:
            self._log(f"[ERR] {pkg_name} 强制下载失败: {exc}")
            QMessageBox.warning(self, "下载失败", f"{pkg_name} 强制下载失败:\n{exc}")
            return False

        self._log(f"[ok] {pkg_name} 强制下载完成（已覆盖本地）")
        return True

    def _clip_lines(self, lines: list[str], limit: int = PREVIEW_MAX_LINES) -> list[str]:
        clipped = [x for x in lines if x is not None]
        if len(clipped) > limit:
            return clipped[:limit] + [f"...(共 {len(clipped)} 行，仅显示前 {limit} 行)"]
        return clipped

    def _confirm_action(
        self,
        title: str,
        pkg_name: str,
        lines: list[str],
        fallback: str,
        enable_ai: bool = False,
        ai_merge_plan: dict | None = None,
        ai_merge_pkg_dir: Path | None = None,
        file_apply_pkg_dir: Path | None = None,
        file_apply_upstream_ref: str | None = None,
    ) -> tuple[bool, str | None]:
        preview = [x for x in lines if x.strip()]
        if not preview:
            preview = [fallback]
        clipped = self._clip_lines(preview)

        summary = []
        deletion_summary: list[str] = []
        for line in clipped:
            if self._is_deletion_line(line):
                deletion_summary.append(line.strip())
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith("["):
                continue
            if stripped.startswith("?? "):
                summary.append(line)
                continue
            if "\t" in stripped:
                code = stripped.split("\t", 1)[0].strip().upper()
                if code in ("A", "M", "D", "R", "C"):
                    summary.append(line)
                continue
            parts = stripped.split(maxsplit=1)
            if parts and len(parts[0]) <= 2 and any(c in parts[0] for c in "MADRC"):
                summary.append(line)
        summary = summary[:25]
        summary_text = "\n".join(summary) if summary else "(见详细变更)"
        deletion_count = len(deletion_summary)

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(980, 720)
        dlg.setMinimumSize(860, 620)
        layout = QVBoxLayout(dlg)

        info_lines = [f"{pkg_name} 将执行文件同步。"]
        if deletion_count:
            info_lines.append(
                f"\n⚠ 注意：有 {deletion_count} 个本地已删除的文件将一并提交，远端对应文件也会被删除。"
            )
        info_lines.append("\n下方按标签页显示完整预览（含完整路径与 diff）。是否继续？")
        info = QLabel("".join(info_lines))
        info.setWordWrap(True)
        if deletion_count:
            info.setStyleSheet("color: #b45309; font-weight: bold;")
        layout.addWidget(info)

        if deletion_count:
            shown = "\n".join(deletion_summary[:15])
            if deletion_count > 15:
                shown += f"\n...(共 {deletion_count} 个删除，见总览)"
            blocks = [f"⚠ 将删除的文件 ({deletion_count}):\n{shown}"]
            if summary:
                blocks.append(f"关键文件变更（新增/修改/删除）:\n{summary_text}")
            summary_plain = "\n\n".join(blocks)
        else:
            summary_plain = f"关键文件变更（新增/修改/删除）:\n{summary_text}"
        summary_box = QTextEdit(dlg)
        summary_box.setReadOnly(True)
        summary_box.setPlainText(summary_plain)
        summary_box.setMinimumHeight(120)
        summary_box.setMaximumHeight(500)
        layout.addWidget(summary_box)

        tabs = QTabWidget(dlg)
        tabs.setMinimumHeight(500)

        overview = QTextEdit(dlg)
        overview.setReadOnly(True)
        overview.setPlainText("\n".join(clipped))
        self._apply_diff_highlight(overview)
        tabs.addTab(overview, "总览")

        upstream_ref = (file_apply_upstream_ref or "").strip()
        can_apply_single_file = bool(file_apply_pkg_dir and upstream_ref)

        file_blocks = self._extract_file_diff_blocks(preview)
        if not file_blocks:
            file_blocks = self._extract_status_file_blocks(preview)
        for file_name, block in file_blocks:
            # 尝试从 diff block 解析相对路径，用于“单文件合并到本地”
            relpath: str | None = None
            first = (block.splitlines()[0].strip() if block else "")
            if first.startswith("diff --git "):
                parts = first.split()
                if len(parts) >= 4 and parts[2].startswith("a/"):
                    relpath = parts[2][2:]

            file_tab = QWidget(dlg)
            file_lay = QVBoxLayout(file_tab)
            file_lay.setContentsMargins(0, 0, 0, 0)
            file_lay.setSpacing(8)

            if can_apply_single_file and relpath:
                bar = QHBoxLayout()
                bar.setSpacing(8)
                btn_apply = QPushButton("仅合并该文件到本地（取远端版本）")
                lbl_apply = QLabel("将远端版本写入本地工作区（不执行整体下载/合并流程）。")
                lbl_apply.setWordWrap(True)
                bar.addWidget(btn_apply)
                bar.addWidget(lbl_apply, 1)
                file_lay.addLayout(bar)

                def _apply_one(_checked=False, _relpath=relpath):
                    btn_apply.setEnabled(False)
                    lbl_apply.setText("应用中…")

                    def _worker():
                        ok, msg = self._run(
                            ["git", "checkout", upstream_ref, "--", _relpath],
                            cwd=file_apply_pkg_dir,
                        )
                        if ok:
                            self._log(
                                f"[ok] 单文件写入本地: {_relpath} ← {upstream_ref}")

                        def _update_ui():
                            # 对话框可能已关闭、控件已 deleteLater，访问已删除的
                            # C++ 对象会直接 abort（非 Python 异常，无法 try 捕获）。
                            if not _qt_alive(btn_apply):
                                return
                            if ok:
                                lbl_apply.setText("已写入本地工作区。")
                            else:
                                lbl_apply.setText("应用失败。")
                                QMessageBox.warning(
                                    dlg if _qt_alive(dlg) else self,
                                    "单文件合并到本地失败",
                                    f"{pkg_name}\n文件: {_relpath}\nref: {upstream_ref}\n\n{msg}",
                                )
                            btn_apply.setEnabled(True)

                        self._post_main(_update_ui)

                    self._start_worker(_worker)

                btn_apply.clicked.connect(_apply_one)

            editor = QTextEdit(dlg)
            editor.setReadOnly(True)
            editor.setPlainText(block)
            self._apply_diff_highlight(editor)
            file_lay.addWidget(editor, 1)
            tabs.addTab(file_tab, self._safe_tab_name(file_name))

        # ---- AI 合并：为需要合并（双方都改）的文件增加“合并结果预览”页 ----
        if ai_merge_plan and ai_merge_pkg_dir:
            try:
                both_files = list(ai_merge_plan.get("both") or [])
                upstream = (ai_merge_plan.get("upstream") or "").strip()
                merge_base = (ai_merge_plan.get("merge_base") or "").strip()
            except Exception:
                both_files, upstream, merge_base = [], "", ""

            if both_files and upstream and merge_base:
                preview_bridge = _AiMergePreviewBridge()
                preview_widgets: dict[
                    str, tuple[QPushButton, QLabel,
                               QTextEdit, QPushButton, dict]
                ] = {}

                def _on_preview_ready(relpath: str, ok: bool, text: str):
                    w = preview_widgets.get(relpath)
                    if not w:
                        return
                    btn, status, editor, btn_write, state = w
                    if not _qt_alive(btn):
                        return
                    btn.setEnabled(True)
                    if ok:
                        status.setText("已生成合并预览（未写入磁盘）。")
                        editor.setPlainText(text or "")
                        state["merged"] = text or ""
                        btn_write.setEnabled(True)
                    else:
                        status.setText("生成失败。")
                        editor.setPlainText(text or "生成失败（无错误信息）")
                        state["merged"] = ""
                        btn_write.setEnabled(False)

                preview_bridge.finished.connect(_on_preview_ready)

                # 对话框关闭后断开连接：AI worker 可能仍在后台运行并迟到 emit，
                # 此时对话框控件已 deleteLater，槽函数访问会触发 abort。
                def _disconnect_preview_bridge(*_a):
                    try:
                        preview_bridge.finished.disconnect()
                    except (RuntimeError, TypeError):
                        pass
                dlg.finished.connect(_disconnect_preview_bridge)

                for relpath in both_files:
                    file_tab = QWidget(dlg)
                    file_lay = QVBoxLayout(file_tab)
                    file_lay.setContentsMargins(0, 0, 0, 0)
                    file_lay.setSpacing(8)

                    top_bar = QHBoxLayout()
                    top_bar.setSpacing(8)
                    btn_preview = QPushButton("生成合并结果预览")
                    btn_write = QPushButton("将合并结果写入本地")
                    btn_write.setEnabled(False)
                    lbl_status = QLabel("此文件存在冲突需要合并。点击按钮生成 AI 合并后的结果预览（不落盘）。")
                    lbl_status.setWordWrap(True)
                    top_bar.addWidget(btn_preview)
                    top_bar.addWidget(btn_write)
                    top_bar.addWidget(lbl_status, 1)
                    file_lay.addLayout(top_bar)

                    preview_edit = QTextEdit(dlg)
                    preview_edit.setReadOnly(True)
                    preview_edit.setPlaceholderText("合并结果预览将显示在这里。")
                    file_lay.addWidget(preview_edit, 1)

                    state = {"merged": ""}
                    preview_widgets[relpath] = (
                        btn_preview, lbl_status, preview_edit, btn_write, state)

                    def _start_generate(_checked=False, _relpath=relpath):
                        btn, status, editor = preview_widgets.get(
                            _relpath, (None, None, None))
                        if not btn or not status or not editor:
                            return
                        btn.setEnabled(False)
                        status.setText("生成中…（可能需要几十秒）")
                        editor.setPlainText("")

                        def _worker():
                            try:
                                local_text, remote_text, base_text = self._read_merge_versions(
                                    ai_merge_pkg_dir, upstream, merge_base, _relpath
                                )
                                if local_text is None or remote_text is None:
                                    preview_bridge.finished.emit(
                                        _relpath,
                                        False,
                                        "无法读取本地/远端版本（可能为二进制文件或 git 无法读取）。",
                                    )
                                    return
                                ok, merged = self._ai_merge_file_content(
                                    _relpath, local_text, remote_text, base_text
                                )
                                preview_bridge.finished.emit(
                                    _relpath, ok, merged)
                            except Exception as exc:
                                preview_bridge.finished.emit(
                                    _relpath, False, f"异常：{exc}")

                        self._start_worker(_worker)

                    btn_preview.clicked.connect(_start_generate)

                    def _write_to_local(_checked=False, _relpath=relpath):
                        w = preview_widgets.get(_relpath)
                        if not w:
                            return
                        _, status, _, btn_w, st = w
                        merged = (st.get("merged") or "").rstrip("\n")
                        if not merged:
                            QMessageBox.information(
                                dlg, "写入失败", f"{_relpath}\n尚未生成合并结果预览。")
                            return
                        btn_w.setEnabled(False)
                        status.setText("写入本地中…")
                        ok = self._write_merged_file(
                            ai_merge_pkg_dir, _relpath, merged + "\n")
                        if ok:
                            status.setText("已写入本地工作区（未提交）。")
                            self._log(f"[ok] AI 合并结果写入本地: {_relpath}")
                        else:
                            status.setText("写入失败。")
                            QMessageBox.warning(
                                dlg, "写入失败", f"{_relpath}\n写入本地失败，请查看日志。")
                        btn_w.setEnabled(True)

                    btn_write.clicked.connect(_write_to_local)
                    tabs.addTab(file_tab, self._safe_tab_name(
                        f"[合并预览] {Path(relpath).name}"))

        main_content_splitter = QSplitter(Qt.Horizontal, dlg)
        main_content_splitter.setChildrenCollapsible(False)
        main_content_splitter.addWidget(tabs)

        ai_detail_edit: QTextEdit | None = None
        ai_use_as_commit = None
        if enable_ai:
            ai_panel = QWidget(dlg)
            ai_panel_lay = QVBoxLayout(ai_panel)
            ai_panel_lay.setContentsMargins(0, 0, 0, 0)
            ai_panel_lay.setSpacing(8)

            ai_bar = QHBoxLayout()
            ai_bar.setSpacing(8)
            ai_btn = QPushButton("AI查看修改详情")
            ai_status = QLabel("可生成详细分析，并可直接用作提交注释。支持流式输出。")
            ai_status.setWordWrap(True)
            ai_bar.addWidget(ai_btn)
            ai_bar.addWidget(ai_status, 1)
            ai_panel_lay.addLayout(ai_bar)

            token_line = QHBoxLayout()
            token_line.setSpacing(8)
            in_token_label = QLabel("输入Token(估算): 0")
            out_token_label = QLabel("输出Token(估算): 0")
            token_line.addWidget(in_token_label)
            token_line.addWidget(out_token_label)
            token_line.addStretch()
            ai_panel_lay.addLayout(token_line)

            ai_input_edit = QTextEdit(dlg)
            ai_input_edit.setReadOnly(True)
            ai_input_edit.setPlaceholderText("这里显示发送给 AI 的输入详情（prompt）。")
            ai_input_edit.setMinimumHeight(120)
            ai_panel_lay.addWidget(ai_input_edit)

            ai_detail_edit = QTextEdit(dlg)
            ai_detail_edit.setPlaceholderText("点击“AI查看修改详情”后将在此显示分析结果。")
            ai_detail_edit.setMinimumHeight(220)
            ai_panel_lay.addWidget(ai_detail_edit, 1)

            ai_trace_edit = QTextEdit(dlg)
            ai_trace_edit.setReadOnly(True)
            ai_trace_edit.setPlaceholderText("实时状态日志")
            ai_trace_edit.setMinimumHeight(90)
            ai_panel_lay.addWidget(ai_trace_edit)

            ai_use_as_commit = QCheckBox("提交时使用 AI 详情作为 commit message")
            ai_use_as_commit.setChecked(True)
            ai_panel_lay.addWidget(ai_use_as_commit)

            stream_bridge = _AiStreamBridge()
            current_ai_text = {"text": ""}

            def _append_status(msg: str):
                if not _qt_alive(ai_trace_edit):
                    return
                ai_trace_edit.append(msg)
                ai_trace_edit.ensureCursorVisible()

            def _append_chunk(chunk: str):
                if not chunk or not _qt_alive(ai_detail_edit):
                    return
                current_ai_text["text"] += chunk
                ai_detail_edit.setPlainText(current_ai_text["text"])
                out_token_label.setText(
                    f"输出Token(估算): {self._estimate_token_count(current_ai_text['text'])}"
                )
                cursor = ai_detail_edit.textCursor()
                cursor.movePosition(cursor.MoveOperation.End)
                ai_detail_edit.setTextCursor(cursor)

            def _finish_stream(ok: bool, message: str):
                if not _qt_alive(ai_btn):
                    return
                ai_btn.setEnabled(True)
                ai_btn.setText("AI查看修改详情")
                if ok:
                    if message and not current_ai_text["text"]:
                        current_ai_text["text"] = message
                        ai_detail_edit.setPlainText(message)
                    ai_status.setText("AI 分析完成，可编辑后作为提交注释。")
                else:
                    ai_status.setText("AI 分析失败，请检查 Key/模型配置。")
                    if message:
                        _append_status(f"[ERROR] {message}")

            stream_bridge.status.connect(_append_status)
            stream_bridge.chunk.connect(_append_chunk)
            stream_bridge.finished.connect(_finish_stream)

            # 对话框关闭后断开连接：流式 AI worker（urllib 读最长 90s）可能在
            # 用户点 Yes 后仍在后台运行并迟到 emit chunk/status/finished，
            # 而对话框控件此时已 deleteLater，槽函数访问已删除控件 → abort。
            # worker 闭包持有 stream_bridge 引用，断开后迟到 emit 安全落空。
            def _disconnect_stream_bridge(*_a):
                for _sig in (stream_bridge.status, stream_bridge.chunk,
                             stream_bridge.finished):
                    try:
                        _sig.disconnect()
                    except (RuntimeError, TypeError):
                        pass
            dlg.finished.connect(_disconnect_stream_bridge)

            def _run_ai_review():
                ai_btn.setEnabled(False)
                ai_btn.setText("分析中...")
                ai_trace_edit.clear()
                current_ai_text["text"] = ""
                ai_detail_edit.clear()

                ai_input_lines = self._clip_lines(clipped, limit=1200)
                prompt = self._build_ai_detailed_review_prompt(
                    pkg_name, "\n".join(ai_input_lines))
                ai_input_edit.setPlainText(prompt)
                in_token_label.setText(
                    f"输入Token(估算): {self._estimate_token_count(prompt)}")
                out_token_label.setText("输出Token(估算): 0")
                stream_bridge.status.emit("[INFO] 已构建输入，开始请求 AI（stream）。")

                def _worker():
                    ok, msg = self._stream_ai_detailed_review(
                        prompt=prompt,
                        on_status=lambda s: stream_bridge.status.emit(s),
                        on_chunk=lambda c: stream_bridge.chunk.emit(c),
                    )
                    stream_bridge.finished.emit(ok, msg)

                self._start_worker(_worker)

            ai_btn.clicked.connect(_run_ai_review)
            main_content_splitter.addWidget(ai_panel)
            main_content_splitter.setStretchFactor(0, 3)
            main_content_splitter.setStretchFactor(1, 2)
            main_content_splitter.setSizes([640, 420])
        else:
            main_content_splitter.setStretchFactor(0, 1)

        layout.addWidget(main_content_splitter, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_yes = QPushButton("Yes")
        btn_no = QPushButton("No")
        btn_no.setDefault(True)
        btn_yes.clicked.connect(dlg.accept)
        btn_no.clicked.connect(dlg.reject)
        btn_row.addWidget(btn_yes)
        btn_row.addWidget(btn_no)
        layout.addLayout(btn_row)
        list_scroll = self._list_scroll_pos()
        accepted = dlg.exec() == QDialog.Accepted
        self._restore_list_scroll(list_scroll)
        commit_msg_from_ai = None
        if (
            accepted
            and enable_ai
            and ai_detail_edit is not None
            and ai_use_as_commit is not None
            and ai_use_as_commit.isChecked()
        ):
            txt = (ai_detail_edit.toPlainText() or "").strip()
            if txt:
                commit_msg_from_ai = txt
        return accepted, commit_msg_from_ai

    @staticmethod
    def _safe_tab_name(file_name: str, max_len: int = 36) -> str:
        """标签页名称截断，避免过长影响可读性。"""
        if len(file_name) <= max_len:
            return file_name
        return "..." + file_name[-(max_len - 3):]

    @staticmethod
    def _extract_file_diff_blocks(lines: list[str]) -> list[tuple[str, str]]:
        """从预览文本中提取每个文件的 diff 块，用于标签页展示。"""
        blocks: list[tuple[str, str]] = []
        current_name: str | None = None
        current_lines: list[str] = []

        for line in lines:
            if line.startswith("[未跟踪文件 diff] "):
                if current_name and current_lines:
                    blocks.append((current_name, "\n".join(current_lines)))
                abs_path = line.split("] ", 1)[
                    1].strip() if "] " in line else line
                current_name = Path(
                    abs_path).name or f"untracked_{len(blocks) + 1}"
                current_lines = [line]
                continue
            if line.startswith("diff --git "):
                if current_name and current_lines:
                    blocks.append((current_name, "\n".join(current_lines)))
                current_lines = [line]
                parts = line.split()
                if len(parts) >= 4 and parts[2].startswith("a/"):
                    rel_path = parts[2][2:]
                    current_name = Path(rel_path).name
                else:
                    current_name = f"file_{len(blocks) + 1}"
                continue
            if current_name:
                current_lines.append(line)

        if current_name and current_lines:
            blocks.append((current_name, "\n".join(current_lines)))
        return blocks

    @staticmethod
    def _extract_status_file_blocks(lines: list[str]) -> list[tuple[str, str]]:
        """无 diff 块时，从 status / name-status 行生成按文件展示的标签页内容。"""
        blocks: list[tuple[str, str]] = []
        seen: set[str] = set()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("[") or stripped.startswith("("):
                continue
            path = stripped
            if "\t" in stripped:
                parts = stripped.split("\t", 1)
                path = parts[1].strip() if len(parts) > 1 else stripped
            elif stripped.startswith("?? "):
                path = stripped[3:].strip()
            elif len(stripped) > 3 and stripped[2] == " ":
                path = stripped[3:].strip()
            elif " " in stripped:
                head, rest = stripped.split(maxsplit=1)
                if len(head) <= 2 and head.strip():
                    path = rest.strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[-1].strip()
            path = path.strip().strip('"')
            if not path or path in seen:
                continue
            seen.add(path)
            name = Path(path.replace("\\", "/")).name or path
            blocks.append((name, stripped))
        return blocks[:80]

    @staticmethod
    def _apply_diff_highlight(editor: QTextEdit):
        highlighter = _DiffHighlighter(editor.document())
        setattr(editor, "_diff_highlighter", highlighter)

    def _build_ai_detailed_review_prompt(self, pkg_name: str, preview_text: str) -> str:
        """构建 AI 详细分析 prompt。"""
        return (
            "请基于以下 git 变更预览，输出尽量详细的改动分析，并生成可直接用于 git commit -m 的提交注释。\n"
            "输出格式要求：\n"
            "1) 第一行：简洁标题（不超过 50 字）。\n"
            "2) 空一行后，详细说明改动点、影响范围、风险点、验证建议（每项用短段落）。\n"
            "3) 内容直接输出纯文本，不要 markdown 代码块。\n\n"
            f"包名: {pkg_name}\n\n"
            "变更预览:\n"
            f"{preview_text}"
        )

    @staticmethod
    def _estimate_token_count(text: str) -> int:
        """简易 token 估算：按 UTF-8 字节长度约 4 字节/token。"""
        if not text:
            return 0
        return max(1, len(text.encode("utf-8")) // 4)

    def _stream_ai_detailed_review(
        self,
        prompt: str,
        on_status,
        on_chunk,
    ) -> tuple[bool, str]:
        """流式请求 AI 详细分析，实时回调输出。"""
        cfg = self._ai_config or {}
        if not cfg.get("enabled", True):
            return False, "AI 模型已禁用，请在「模型设置…」中启用"
        chat_url, api_key, model = self._get_ai_endpoint()
        if not api_key:
            return False, "未填写 AI API Key，无法生成详细分析"
        if not chat_url:
            return False, "未配置 Base URL，无法生成详细分析"
        payload = {
            "model": model,
            "stream": True,
            "messages": [
                {"role": "system", "content": "你是资深代码审查助手，擅长撰写高质量提交注释。"},
                {"role": "user", "content": prompt},
            ],
        }
        req = urllib.request.Request(
            chat_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            on_status("[INFO] 请求已发送，等待流式响应...")
            chunks: list[str] = []
            with _siliconflow_urlopen(req, 90) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        on_status("[INFO] 流式输出结束。")
                        break
                    try:
                        packet = json.loads(data_str)
                    except Exception:
                        continue
                    delta = (
                        packet.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content", "")
                    )
                    if delta:
                        chunks.append(delta)
                        # 逐字输出，确保可见“流式打字”效果。
                        for ch in delta:
                            on_chunk(ch)
                            time.sleep(0.005)
            content = "".join(chunks).strip()
            if not content:
                return False, "AI 返回为空"
            return True, content
        except Exception as exc:
            return False, f"AI 详细分析失败: {exc}"

    def _status_lines(self, pkg_dir: Path) -> tuple[bool, list[str]]:
        repo = self._repo(pkg_dir)
        if repo is not None:
            try:
                out = repo.git.status(porcelain=True)
                lines = [line for line in (
                    out or "").splitlines() if line.strip()]
                return True, lines
            except Exception:
                pass
        ok, msg = self._run(["git", "status", "--porcelain"], cwd=pkg_dir)
        if not ok:
            return False, [f"[无法获取 status] {msg}"]
        lines = [line for line in (msg or "").splitlines() if line.strip()]
        return True, lines

    def _resolve_compare_ref(self, pkg_dir: Path) -> tuple[str | None, str]:
        """解析上传对比基准：上游分支或 origin/<当前分支>。"""
        ok_up, upstream = self._run(
            ["git", "rev-parse", "--abbrev-ref",
                "--symbolic-full-name", "@{u}"],
            cwd=pkg_dir,
        )
        if ok_up and (upstream or "").strip():
            ref = upstream.strip().splitlines()[-1]
            return ref, f"上游 {ref}"

        branch = self._current_branch(pkg_dir)
        remote_ref = f"origin/{branch}"
        ok_ref, _ = self._run(
            ["git", "rev-parse", "--verify", remote_ref], cwd=pkg_dir)
        if ok_ref:
            return remote_ref, f"远端 {remote_ref}"
        return None, "远端尚无对应分支（首次推送）"

    def _append_first_push_preview(self, out: list[str], pkg_dir: Path) -> None:
        """无远端基准时：展示本地待推送提交与将推送的文件。"""
        ok_log, msg_log = self._run(
            ["git", "log", "--oneline", "-20"], cwd=pkg_dir)
        out.append("")
        out.append("[本地待推送提交]")
        log_lines = [line for line in (
            msg_log or "").splitlines() if line.strip()]
        out.extend(log_lines or ["(无提交记录 — 仅会推送未提交的工作区改动)"])

        empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        out.append("")
        out.append("[将推送的文件列表 name-status]")
        ok_ns, msg_ns = self._run(
            ["git", "diff", "--name-status", empty_tree, "HEAD"], cwd=pkg_dir
        )
        if ok_ns and (msg_ns or "").strip():
            out.extend(msg_ns.strip().splitlines())
        else:
            ok_tree, msg_tree = self._run(
                ["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=pkg_dir
            )
            tree_lines = [line for line in (
                msg_tree or "").splitlines() if line.strip()]
            out.extend(tree_lines or ["(HEAD 中无已跟踪文件)"])

        out.append("")
        ok_stat, msg_stat = self._run(
            ["git", "diff", "--stat", empty_tree, "HEAD"], cwd=pkg_dir
        )
        if ok_stat and (msg_stat or "").strip():
            out.append("[将推送的变更统计]")
            out.extend(msg_stat.strip().splitlines())
            out.append("")

        out.extend(
            self._diff_lines(
                pkg_dir,
                ["git", "diff", "--patch", empty_tree, "HEAD"],
                "将推送到远端的完整 diff",
            )
        )

    def _diff_lines(self, pkg_dir: Path, diff_cmd: list[str], title: str) -> list[str]:
        ok, msg = self._run(diff_cmd, cwd=pkg_dir)
        if not ok:
            return [f"[{title}] 获取失败: {msg}"]
        body = [line for line in (msg or "").splitlines()]
        if not body:
            return [f"[{title}] (无差异)"]
        return [f"[{title}]"] + body

    def _preview_upload_files(self, pkg_dir: Path) -> list[str]:
        """上传确认显示远端差异，并补充本地未提交改动提示。"""
        out: list[str] = []
        out.append(
            "[INFO] 上传预览不执行 git fetch，避免网络慢导致界面卡顿；远端差异使用本地缓存的 origin 引用。")
        out.append("")
        compare_ref, compare_desc = self._resolve_compare_ref(pkg_dir)
        ok_status, status_lines = self._status_lines(pkg_dir)

        if compare_ref:
            out.extend(
                [f"[远端差异预览] 本地 HEAD 相对 {compare_desc}",
                    "", "[文件列表: name-status]"]
            )
            ok_name, msg_name = self._run(
                ["git", "diff", "--name-status", f"{compare_ref}..HEAD"], cwd=pkg_dir
            )
            if not ok_name:
                out.append(f"[远端差异预览] 获取失败: {msg_name}")
            else:
                lines = [line for line in (
                    msg_name or "").splitlines() if line.strip()]
                out.extend(lines or ["(当前无已提交差异可推送)"])
            out.append("")
            out.extend(
                self._diff_lines(
                    pkg_dir,
                    ["git", "diff", "--patch", f"{compare_ref}..HEAD"],
                    "将推送到远端的提交差异 diff",
                )
            )
        else:
            out.append(f"[远端差异预览] {compare_desc}。")
            self._append_first_push_preview(out, pkg_dir)

        self._append_local_pending_preview(
            out, pkg_dir, ok_status, status_lines)
        if ok_status and status_lines:
            out.append("")
            out.append("[工作区文件一览 status]")
            out.extend(
                self._format_status_lines_with_full_path(pkg_dir, status_lines)
            )
        return out

    def _preview_package_list_progress(self, package_entries: list[tuple[str, Path, bool]], ready: set[str]):
        if not self.list_layout:
            return
        for pkg_name, _, remote_only in package_entries:
            if remote_only or pkg_name in ready:
                continue
            label = self.package_status_labels.get(pkg_name)
            if label is not None:
                label.setText(f"{pkg_name} [扫描中…]")
                label.setStyleSheet("font-size: 12px; color: #9aa0a6;")

    def _log_upload_summary(self, pkg_dir: Path, pkg_name: str) -> None:
        """上传成功后显示提交注释和文件列表。"""
        # 获取最新 commit message
        ok_msg, commit_msg = self._run(
            ["git", "log", "-1", "--format=%B"], cwd=pkg_dir
        )
        if ok_msg and commit_msg:
            self._log(f"[提交注释] {pkg_name}:")
            for line in commit_msg.strip().splitlines():
                self._log(f"  {line}")

        # 获取最新 commit 的文件列表
        ok_files, files_output = self._run(
            ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", "HEAD"],
            cwd=pkg_dir,
        )
        if ok_files and files_output and files_output.strip():
            self._log(f"[上传文件] {pkg_name}:")
            for line in files_output.strip().splitlines():
                self._log(f"  {line}")
        elif ok_files:
            self._log(f"[上传文件] {pkg_name}: (本次提交无文件变化)")

    def _format_status_lines_with_full_path(self, pkg_dir: Path, status_lines: list[str]) -> list[str]:
        """将 git status --porcelain 输出转换为带完整路径的可读行。"""
        out: list[str] = []
        for raw in status_lines:
            parsed = self._parse_porcelain_line(raw)
            if not parsed:
                continue
            status_raw, path_part = parsed
            status = status_raw.strip() or "?"

            if " -> " in path_part:
                old_rel, new_rel = path_part.split(" -> ", 1)
                old_abs = str((pkg_dir / old_rel).resolve())
                new_abs = str((pkg_dir / new_rel).resolve())
                out.append(f"{status} {old_abs} -> {new_abs}")
                continue

            rel = path_part
            if rel.startswith('"') and rel.endswith('"') and len(rel) >= 2:
                rel = rel[1:-1]
            abs_path = str((pkg_dir / rel).resolve())
            out.append(f"{status} {abs_path}")
        return out

    def _local_uncommitted_diff_lines(self, pkg_dir: Path, status_lines: list[str]) -> list[str]:
        """返回本地未提交改动 diff，含 ?? 未跟踪文件内容。"""
        out: list[str] = []
        ok_unstaged, msg_unstaged = self._run(
            ["git", "diff", "--patch"], cwd=pkg_dir)
        if ok_unstaged and (msg_unstaged or "").strip():
            out.append("[未暂存改动 diff]")
            out.extend(msg_unstaged.strip().splitlines())
            out.append("")

        ok_staged, msg_staged = self._run(
            ["git", "diff", "--cached", "--patch"], cwd=pkg_dir)
        if ok_staged and (msg_staged or "").strip():
            out.append("[已暂存改动 diff]")
            out.extend(msg_staged.strip().splitlines())
            out.append("")

        for raw in status_lines:
            parsed = self._parse_porcelain_line(raw)
            if not parsed or parsed[0] != "??":
                continue
            rel = parsed[1].strip()
            if rel.startswith('"') and rel.endswith('"') and len(rel) >= 2:
                rel = rel[1:-1]
            file_path = (pkg_dir / rel).resolve()
            if not file_path.is_file():
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
                content_lines = text.splitlines()
                diff_lines = list(
                    difflib.unified_diff(
                        [],
                        content_lines,
                        fromfile="/dev/null",
                        tofile=f"b/{rel.replace(os.sep, '/')}",
                        lineterm="",
                    )
                )
                if diff_lines:
                    out.append(f"[未跟踪文件 diff] {file_path}")
                    out.extend(diff_lines)
                    out.append("")
            except Exception as exc:
                out.append(f"[未跟踪文件 diff] 读取失败: {file_path} ({exc})")
                out.append("")
        return out

    def _preview_download_files(self, pkg_dir: Path) -> list[str]:
        self._run(["git", "fetch", "--all", "--prune"], cwd=pkg_dir)
        ok_up, upstream = self._run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=pkg_dir
        )
        if not ok_up or not upstream.strip():
            return ["[无上游分支] 将尝试 git pull --ff-only"]
        upstream_ref = upstream.strip().splitlines()[-1]
        ok_diff, msg_diff = self._run(
            ["git", "diff", "--name-status", f"HEAD..{upstream_ref}"], cwd=pkg_dir
        )
        if not ok_diff:
            return [f"[无法预览] {msg_diff}"]
        lines = [line for line in (
            msg_diff or "").splitlines() if line.strip()]
        out = [f"[下载预览] 远端 {upstream_ref} -> 本地 HEAD",
               "", "[文件列表: name-status]"]
        out.extend(lines or ["(无远端文件变化)"])
        out.append("")
        out.extend(
            self._diff_lines(
                pkg_dir,
                ["git", "diff", "--patch", f"HEAD..{upstream_ref}"],
                "远端将带来的修改 diff",
            )
        )
        return out

    def _preview_force_download_conflicts(self, pkg_dir: Path, upstream_ref: str | None = None) -> list[str]:
        """展示强制覆盖前的本地冲突详情：本地未提交改动 + 与远端的差异。"""
        out: list[str] = []
        ok_status, status_lines = self._status_lines(pkg_dir)
        if ok_status:
            out.append("[本地未提交改动]")
            local_pending = self._format_status_lines_with_full_path(
                pkg_dir, status_lines)
            out.extend(local_pending or ["(无本地未提交改动)"])
            if local_pending:
                out.append("")
                out.extend(self._local_uncommitted_diff_lines(
                    pkg_dir, status_lines))
        else:
            out.append("[本地未提交改动] 获取失败")
            out.extend(status_lines)

        out.append("")
        ok_up, upstream = self._run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=pkg_dir
        )
        upstream_text = upstream_ref or (upstream.strip().splitlines(
        )[-1] if ok_up and upstream and upstream.strip() else "")
        if upstream_text:
            out.append(f"[远端将覆盖到的版本] {upstream_text}")
            out.append("")
            out.extend(
                self._diff_lines(
                    pkg_dir,
                    ["git", "diff", "--patch", f"HEAD..{upstream_text}"],
                    "远端将带来的修改 diff",
                )
            )
        else:
            out.append("[远端将覆盖到的版本] 无上游分支，后续会回退到 origin/<当前分支>")
        return out

    def upload_one(self, pkg_dir: Path):
        owner = self.owner_edit.text().strip() or "Lugwit123"
        pkg_name = pkg_dir.name

        if not self._check_tools():
            return
        self._set_busy(True)
        try:
            self._log(f"========== 上传 {pkg_name} ==========")
            self._log_phase_start(f"上传 {pkg_name}")
            if not self._ensure_git_repo(pkg_dir):
                return
            self._ensure_gitignore(pkg_dir)
            self._ensure_remote(pkg_dir, pkg_name, owner)
            upload_preview = self._preview_upload_files(pkg_dir)
            confirmed, ai_commit_message = self._confirm_action(
                "确认上传",
                pkg_name,
                upload_preview,
                "无文件变化（可能仅推送远端分支状态）",
                enable_ai=True,
            )
            if not confirmed:
                self._log(f"[info] {pkg_name} 取消上传")
                return
            branch = self._current_branch(pkg_dir)

            ok_status, status_lines = self._status_lines(pkg_dir)
            pending_deletions = (
                self._collect_deletion_paths(
                    pkg_dir, status_lines) if ok_status else []
            )
            if pending_deletions:
                sample = ", ".join(pending_deletions[:3])
                if len(pending_deletions) > 3:
                    sample += " ..."
                self._log(
                    f"[info] {pkg_name} 将提交 {len(pending_deletions)} 个文件删除: {sample}"
                )

            self._run(["git", "add", "-A"], cwd=pkg_dir)
            ok, _ = self._run(
                ["git", "diff", "--cached", "--quiet"], cwd=pkg_dir)
            if not ok:
                ai_commit_msg = ai_commit_message or self._request_ai_commit_message(
                    pkg_name, pkg_dir)
                commit_msg = ai_commit_msg
                if not commit_msg:
                    manual_msg, accepted = QInputDialog.getMultiLineText(
                        self,
                        "手动输入提交注释",
                        f"{pkg_name} 的 AI 注释不可用，请手动输入 commit message:",
                        f"sync {pkg_name}\n\nmanual commit message",
                    )
                    manual_msg = (manual_msg or "").strip()
                    if not accepted or not manual_msg:
                        self._log(f"[info] {pkg_name} 未提供提交注释，取消上传")
                        return
                    commit_msg = manual_msg
                    self._log(f"[info] {pkg_name} 使用手动提交注释")
                if ai_commit_msg:
                    ai_provider = (self._ai_config or {}).get("provider", "?")
                    ai_model = (self._ai_config or {}).get("model", "?")
                    self._log(
                        f"[ok] {pkg_name} 使用 AI 生成提交注释 (provider={ai_provider}, model={ai_model})")
                commit_msg = commit_msg or ""
                ok_commit, msg_commit, commit_hash = self._run_blocking_with_events(
                    git_helper.commit,
                    pkg_dir,
                    commit_msg,
                )
                if not ok_commit:
                    self._log(f"[WARN] {pkg_name} commit 失败: {msg_commit}")
                elif commit_hash:
                    self._log(f"[ok] {pkg_name} committed: {commit_hash}")
                else:
                    self._log(f"[info] {pkg_name} no changes to commit")
            else:
                self._log(f"[info] {pkg_name} no changes to commit")

            upstream_push_ref: str | None = None
            ok_up, upstream_msg = self._run(
                ["git", "rev-parse", "--verify", f"origin/{branch}"], cwd=pkg_dir
            )
            if ok_up and (upstream_msg or "").strip():
                upstream_push_ref = (
                    upstream_msg or "").strip().splitlines()[-1]
            oversized = self._find_oversized_push_blobs(
                pkg_dir, upstream_push_ref)
            if oversized:
                self._log(
                    f"[ERR] {pkg_name} push 前检测到大文件: "
                    + ", ".join(f"{p} ({self._format_bytes(s)})" for p,
                                s in oversized[:5])
                )
                self._confirm_push_with_oversized_files(pkg_name, oversized)
                return

            ok_push, msg_push = self._run_blocking_with_events(
                git_helper.push,
                pkg_dir,
                branch,
                set_upstream=True,
            )
            if ok_push:
                self._log(f"[ok] {pkg_name} pushed")
                self._log_upload_summary(pkg_dir, pkg_name)
                return

            if self._is_non_fast_forward_error(msg_push):
                self._log(f"[warn] {pkg_name} push 被远端拒绝: {msg_push}")
                if self._ask_force_push(pkg_name, branch, msg_push):
                    oversized = self._find_oversized_push_blobs(
                        pkg_dir, upstream_push_ref)
                    if oversized:
                        self._log(
                            f"[ERR] {pkg_name} 强制推送仍会因大文件失败: "
                            + ", ".join(f"{p} ({self._format_bytes(s)})" for p, s in oversized[:5])
                        )
                        self._confirm_push_with_oversized_files(
                            pkg_name, oversized)
                        return
                    self._log(f"[warn] {pkg_name} 用户确认强制覆盖远端")
                    ok_force, msg_force = self._run_blocking_with_events(
                        git_helper.push,
                        pkg_dir,
                        branch,
                        set_upstream=True,
                        force_with_lease=True,
                    )
                    if ok_force:
                        self._log(f"[ok] {pkg_name} 强制推送成功")
                        self._log_upload_summary(pkg_dir, pkg_name)
                        return
                    self._log(f"[ERR] {pkg_name} 强制推送失败: {msg_force}")
                    QMessageBox.warning(
                        self,
                        "上传失败",
                        f"{pkg_name} 强制推送失败:\n{msg_force}",
                    )
                else:
                    self._log(f"[info] {pkg_name} 用户取消强制上传")
                return

            if not self._should_try_create_repo(msg_push):
                self._log(f"[ERR] {pkg_name} push 失败: {msg_push}")
                hint = ""
                if self._is_git_tooling_error(msg_push):
                    hint = (
                        "（Git 命令在 Windows 下执行异常，请更新本工具后重试；"
                        "若仍失败请检查 Git 是否在 PATH 中。）\n\n"
                    )
                elif "repository not found" not in (msg_push or "").lower():
                    hint = (
                        "远端仓库可能已存在。请检查：网络、GitHub 登录/Token、"
                        "分支是否冲突、是否有未推送的本地提交。\n\n"
                    )
                QMessageBox.warning(
                    self,
                    "上传失败",
                    f"{pkg_name} push 失败。\n\n{hint}详情:\n{msg_push}",
                )
                return

            self._log(f"[info] push 失败，检测为仓库不存在，尝试创建远端仓库: {msg_push}")
            if not self._github_token():
                self._log("[ERR] 未配置 GitHub Token，无法自动创建远端仓库")
                QMessageBox.warning(
                    self,
                    "上传失败",
                    (
                        f"{pkg_name} push 失败，且未配置 GitHub Token。\n"
                        "请点击「网页登录授权」或手动填写 Token 后重试。"
                    ),
                )
                return
            self._try_create_github_repo_and_push(
                pkg_dir, pkg_name, owner, branch)
        finally:
            self._log_phase_end(f"上传 {pkg_name}")
            self._set_busy(False)
            self._update_one_package_status(pkg_name, pkg_dir)

    def download_one(self, pkg_dir: Path):
        owner = self.owner_edit.text().strip() or "Lugwit123"
        pkg_name = pkg_dir.name
        if not self._check_tools():
            return
        self._set_busy(True)
        cloned_new_repo = False
        try:
            self._log(f"========== 下载 {pkg_name} ==========")
            self._log_phase_start(f"下载 {pkg_name}")
            if not pkg_dir.exists():
                pkg_dir.parent.mkdir(parents=True, exist_ok=True)
                ok_clone, msg_clone = self._run(
                    ["git", "clone",
                        f"https://github.com/{owner}/{pkg_name}.git", str(pkg_dir)],
                    safe_dir=pkg_dir,
                )
                if ok_clone:
                    self._log(f"[ok] {pkg_name} cloned")
                    cloned_new_repo = True
                else:
                    self._log(f"[ERR] {pkg_name} clone 失败: {msg_clone}")
                    QMessageBox.warning(
                        self, "下载失败", f"{pkg_name} clone 失败:\n{msg_clone}")
                return

            if not (pkg_dir / ".git").is_dir():
                QMessageBox.warning(
                    self, "下载失败", f"{pkg_name} 目录存在但不是 git 仓库。")
                self._log(f"[ERR] {pkg_name} 目录存在但不是 git 仓库")
                return

            download_preview = self._preview_download_files(pkg_dir)
            ok_up, upstream = self._run(
                ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=pkg_dir
            )
            upstream_ref = upstream.strip().splitlines(
            )[-1] if ok_up and upstream and upstream.strip() else ""
            confirmed, _ = self._confirm_action(
                "确认下载",
                pkg_name,
                download_preview,
                "无远端文件变化",
                enable_ai=False,
                file_apply_pkg_dir=pkg_dir,
                file_apply_upstream_ref=upstream_ref,
            )
            if not confirmed:
                self._log(f"[info] {pkg_name} 取消下载")
                return
            ok_pull, msg_pull = self._run(
                ["git", "pull", "--ff-only"], cwd=pkg_dir)
            if ok_pull:
                self._log(f"[ok] {pkg_name} pulled")
                return

            self._log(f"[warn] {pkg_name} pull --ff-only 失败: {msg_pull}")

            # 合并能力并入「下载」：当处于分叉（双方均有提交）或需要合并时，提供 AI 合并确认并执行。
            # 仅对双方都改的文件显示「合并预览」标签页；对仅远端改动的文件仅显示 diff 标签页。
            if self._ask_force_download(pkg_name, msg_pull):
                self._force_download_workspace(
                    pkg_dir, pkg_name, upstream_ref=upstream_ref)
            else:
                self._log(f"[info] {pkg_name} 用户取消强制下载")
                QMessageBox.warning(
                    self, "下载失败", f"{pkg_name} pull 失败:\n{msg_pull}")
        finally:
            self._log_phase_end(f"下载 {pkg_name}")
            self._set_busy(False)
            if cloned_new_repo:
                self.refresh_packages()
            else:
                self._update_one_package_status(pkg_name, pkg_dir)

    def delete_local_one(self, pkg_dir: Path):
        pkg_name = pkg_dir.name
        if pkg_name in PROTECTED_LOCAL_DELETE:
            QMessageBox.warning(
                self,
                "禁止删除",
                f"不能删除当前工具包 {pkg_name} 的本地目录。",
            )
            return
        if not self._is_managed_package_dir(pkg_dir):
            QMessageBox.warning(self, "删除失败", f"不在允许删除的路径范围内：\n{pkg_dir}")
            return
        if not pkg_dir.exists():
            QMessageBox.information(self, "删除本地", f"{pkg_name} 本地目录不存在。")
            return

        confirm = QMessageBox.question(
            self,
            "确认删除本地",
            (
                f"将永久删除本地目录：\n{pkg_dir}\n\n"
                "远端 GitHub 仓库不受影响。\n"
                "此操作不可恢复，是否继续？"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            self._log(f"[info] {pkg_name} 取消删除本地")
            return

        self._set_busy(True)
        try:
            self._log(f"========== 删除本地 {pkg_name} ==========")
            shutil.rmtree(pkg_dir)
            self._log(f"[ok] {pkg_name} 本地目录已删除: {pkg_dir}")
        except Exception as exc:
            self._log(f"[ERR] {pkg_name} 删除本地失败: {exc}")
            QMessageBox.warning(self, "删除失败", f"{pkg_name} 删除本地失败:\n{exc}")
        finally:
            self._set_busy(False)
            self.refresh_packages()

    def delete_remote_one(self, pkg_dir: Path):
        owner = self.owner_edit.text().strip() or "Lugwit123"
        pkg_name = pkg_dir.name
        if not self._check_tools():
            return

        confirm = QMessageBox.question(
            self,
            "确认删除远端",
            (
                f"将永久删除 GitHub 仓库：\n{owner}/{pkg_name}\n\n"
                "本地目录不受影响。\n"
                "此操作不可恢复，是否继续？"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            self._log(f"[info] {pkg_name} 取消删除远端")
            return

        self._set_busy(True)
        try:
            self._log(f"========== 删除远端 {pkg_name} ==========")
            ok, msg = github_helper.delete_repo(
                owner, pkg_name, self._github_token())
            if ok:
                self._log(f"[ok] {pkg_name} 远端仓库已删除: {owner}/{pkg_name}")
            else:
                self._log(f"[ERR] {pkg_name} 删除远端失败: {msg}")
                QMessageBox.warning(self, "删除失败", f"{pkg_name} 删除远端失败:\n{msg}")
        finally:
            self._set_busy(False)
            self.refresh_packages()

    def upload_all(self):
        if not self._check_tools():
            return
        for _, pkg_dir, remote_only in self._package_entries():
            if remote_only:
                continue
            self.upload_one(pkg_dir)

    def upload_selected(self):
        """上传列表中已勾选的本地包（逐个调用 upload_one）。"""
        if not self._check_tools():
            return
        selected: list[Path] = []
        for name, pkg_dir, remote_only in self._package_entries():
            if remote_only:
                continue
            chk = self.package_checkboxes.get(name)
            if chk is not None and _qt_alive(chk) and chk.isChecked():
                selected.append(pkg_dir)
        if not selected:
            QMessageBox.information(
                self, "上传选中", "未勾选任何可上传的包。\n请在左侧列表勾选要上传的包。")
            return
        self._log(f"[info] 批量上传选中：{len(selected)} 个包")
        for pkg_dir in selected:
            self.upload_one(pkg_dir)

    def download_all(self):
        if not self._check_tools():
            return
        for _, pkg_dir, _remote_only in self._package_entries():
            self.download_one(pkg_dir)

    def restart_self(self):
        """重启程序：先启动新实例，再强制退出当前实例，避免卡在清理阶段。"""
        import shlex

        script_path = Path(__file__).resolve()
        launch_args = list(sys.argv[1:])
        cmd_list = [sys.executable, str(script_path), *launch_args]
        try:
            cmd_display = shlex.join(cmd_list)
        except Exception:
            cmd_display = " ".join(str(x) for x in cmd_list)

        self._log(f"[restart] script_path: {script_path}")
        self._log(f"[restart] launch_args: {launch_args}")
        self._log(f"[restart] cmd_list: {cmd_list}")

        dlg = QDialog(self)
        dlg.setWindowTitle("正在重启 l_repo_sync_gui")
        dlg.setMinimumWidth(560)
        dlg.setModal(True)
        dlg.setWindowFlags(
            dlg.windowFlags()
            & ~Qt.WindowContextHelpButtonHint
            & ~Qt.WindowCloseButtonHint
        )

        lay = QVBoxLayout(dlg)

        lbl_title = QLabel("🔄 重启 l_repo_sync_gui")
        title_font = lbl_title.font()
        title_font.setPointSize(title_font.pointSize() + 2)
        title_font.setBold(True)
        lbl_title.setFont(title_font)
        lay.addWidget(lbl_title)

        lay.addWidget(QLabel("重启命令:"))
        cmd_view = QTextEdit()
        cmd_view.setReadOnly(True)
        cmd_view.setPlainText(cmd_display)
        cmd_view.setMaximumHeight(80)
        cmd_view.setStyleSheet(
            "QTextEdit { background:#1e1e1e; color:#d4d4d4; "
            "font-family: Consolas, 'Courier New', monospace; padding:6px; }"
        )
        lay.addWidget(cmd_view)

        lbl_status = QLabel("⏳ 准备启动新进程…")
        lay.addWidget(lbl_status)

        pb = QProgressBar()
        pb.setRange(0, 0)
        lay.addWidget(pb)

        dlg._proc = None  # type: ignore[attr-defined]
        dlg._spawn_error = ""  # type: ignore[attr-defined]

        def _show_failure(error_text: str):
            pb.setRange(0, 1)
            pb.setValue(0)
            pb.setStyleSheet(
                "QProgressBar::chunk { background-color: #c0392b; }")
            lbl_status.setText(f"❌ 重启失败: {error_text}")
            self._log(f"[ERR] 重启失败: {error_text}")

            err_view = QTextEdit()
            err_view.setReadOnly(True)
            err_view.setPlainText(error_text)
            err_view.setMaximumHeight(90)
            err_view.setStyleSheet(
                "QTextEdit { background:#2b1414; color:#ff8080; "
                "font-family: Consolas, monospace; padding:6px; }"
            )
            lay.addWidget(err_view)

            btn_row = QHBoxLayout()
            btn_row.addStretch()
            btn_copy = QPushButton("复制命令")
            btn_copy.clicked.connect(
                lambda: QApplication.clipboard().setText(cmd_display))
            btn_close = QPushButton("关闭")
            btn_close.setDefault(True)
            btn_close.clicked.connect(dlg.reject)
            btn_row.addWidget(btn_copy)
            btn_row.addWidget(btn_close)
            lay.addLayout(btn_row)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowCloseButtonHint)
            dlg.show()

        def _force_exit_current_app():
            try:
                app = QApplication.instance()
                if app is not None:
                    app.quit()
            finally:
                os._exit(0)

        def _show_success():
            pb.setRange(0, 1)
            pb.setValue(1)
            lbl_status.setText("✅ 新进程已启动，正在退出当前实例…")
            self._log("[ok] 已启动 l_repo_sync_gui 新实例")
            QTimer.singleShot(200, _force_exit_current_app)

        def _spawn():
            lbl_status.setText("⏳ 正在启动新进程…")
            try:
                # 不使用 DETACHED_PROCESS / CREATE_NO_WINDOW，也不重定向 stdin/stdout/stderr。
                # 这样当当前 GUI 是从 PowerShell/cmd 启动时，重启后的子进程会继承同一个控制台，
                # 日志仍然输出到原来的终端；如果当前进程本身没有控制台，则按 Windows 默认行为启动。
                dlg._proc = subprocess.Popen(
                    cmd_list,
                    cwd=str(script_path.parent),
                    env=os.environ.copy(),
                    creationflags=0,
                    close_fds=False,
                )
            except Exception as exc:
                dlg._spawn_error = f"{type(exc).__name__}: {exc}"
                _show_failure(dlg._spawn_error)
                return

            _show_success()

        QTimer.singleShot(200, _spawn)
        dlg.exec()

    def ask_ai(self):
        cfg = self._ai_config or {}
        if not cfg.get("enabled", True):
            QMessageBox.warning(self, "问AI失败", "AI 模型已禁用，请在「模型设置…」中启用。")
            return
        chat_url, api_key, model = self._get_ai_endpoint()
        if not api_key:
            QMessageBox.warning(self, "问AI失败", "请先在「模型设置…」中填写 API Key。")
            return
        if not chat_url:
            QMessageBox.warning(self, "问AI失败", "请先在「模型设置…」中配置 Base URL。")
            return
        user_prompt = self.ai_prompt_edit.toPlainText().strip()
        if not user_prompt:
            QMessageBox.warning(self, "问AI失败", "请先输入提问内容。")
            return
        if self.ai_ask_btn:
            self.ai_ask_btn.setEnabled(False)
            self.ai_ask_btn.setText("请求中...")
        self.ai_answer_edit.setPlainText("请求中，请稍候...")

        def _worker():
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是一个有用的助手"},
                    {"role": "user", "content": user_prompt},
                ],
            }
            req = urllib.request.Request(
                chat_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            try:
                with _siliconflow_urlopen(req, 60) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
                data = json.loads(text)
                choices = data.get("choices") or []
                content = ""
                if choices:
                    content = (
                        choices[0].get("message", {}).get("content", "")
                        or choices[0].get("delta", {}).get("content", "")
                    ).strip()
                if not content:
                    content = f"[接口返回为空]\n{text}"
                self.ai_bridge.finished.emit(True, content)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
                self.ai_bridge.finished.emit(
                    False, f"HTTPError {e.code}: {body}")
            except Exception as e:
                self.ai_bridge.finished.emit(False, repr(e))

        self._start_worker(_worker)

    def _test_model_connection(self):
        cfg = self._ai_config or {}
        if not cfg.get("enabled", True):
            QMessageBox.warning(self, "模型测试失败", "AI 模型已禁用，请在「模型设置…」中启用。")
            return
        chat_url, api_key, model = self._get_ai_endpoint()
        if not api_key:
            QMessageBox.warning(self, "模型测试失败", "请先在「模型设置…」中填写 API Key。")
            return
        if not chat_url:
            QMessageBox.warning(self, "模型测试失败", "请先在「模型设置…」中配置 Base URL。")
            return

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 16,
        }
        req = urllib.request.Request(
            chat_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with _siliconflow_urlopen(req, 10) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            data = json.loads(text)
            choices = data.get("choices") or []
            content = ""
            if choices:
                content = (
                    choices[0].get("message", {}).get("content", "")
                    or choices[0].get("delta", {}).get("content", "")
                ).strip()
            self._log(f"[ok] 模型连接测试成功: {model} @ {cfg.get('provider')}")
            QMessageBox.information(
                self,
                "模型连接测试",
                f"提供商: {cfg.get('provider')}\n模型: {model}\n返回: {content or '(empty)'}",
            )
        except Exception as exc:
            self._log(f"[ERR] 模型连接测试失败: {model} -> {exc}")
            QMessageBox.warning(
                self,
                "模型连接测试失败",
                f"提供商: {cfg.get('provider')}\n模型: {model}\n错误: {exc}",
            )

    def _on_ai_result(self, ok: bool, message: str):
        if self.ai_ask_btn:
            self.ai_ask_btn.setEnabled(True)
            self.ai_ask_btn.setText("问AI")
        self.ai_answer_edit.setPlainText(message)
        if ok:
            self._log("[ok] 问AI完成")
        else:
            self._log(f"[ERR] 问AI失败: {message}")

    def _request_ai_commit_message(self, pkg_name: str, pkg_dir: Path) -> str | None:
        cfg = self._ai_config or {}
        if not cfg.get("enabled", True):
            self._log(f"[WARN] {pkg_name} AI 模型已禁用，将改为手动输入提交注释")
            return None
        _, api_key, _ = self._get_ai_endpoint()
        if not api_key:
            self._log(f"[WARN] {pkg_name} 未填写 AI API Key，将改为手动输入提交注释")
            return None

        ok_name_status, name_status = self._run(
            ["git", "diff", "--cached", "--name-status"], cwd=pkg_dir
        )
        if not ok_name_status:
            return None
        ok_patch_stat, patch_stat = self._run(
            ["git", "diff", "--cached", "--stat"], cwd=pkg_dir
        )
        if not ok_patch_stat:
            patch_stat = ""

        changed = (name_status or "").strip()
        stat = (patch_stat or "").strip()
        if not changed and not stat:
            return None

        prompt = (
            "请基于以下 git 变更生成一个简洁的 commit message。\n"
            "要求：\n"
            "1) 第一行 50 字以内，使用中文或英文均可。\n"
            "2) 空一行后补充 1-2 句说明目的。\n"
            "3) 不要加引号、不要 markdown。\n\n"
            f"包名: {pkg_name}\n\n"
            "变更文件(name-status):\n"
            f"{changed}\n\n"
            "变更统计(stat):\n"
            f"{stat}"
        )

        ok, content = self._call_ai_text(
            system="你是一个有用的助手，擅长写清晰的 git 提交信息。",
            user=prompt,
            timeout=45,
        )
        if not ok or not content:
            if not ok:
                self._log(f"[WARN] AI 生成提交注释失败，将改为手动输入: {content}")
            return content if ok else None
        return content


def _fault_log_path() -> Path:
    """faulthandler 崩溃日志路径，便于排查 PySide6 / Qt 原生层直接退出。"""
    log_dir = Path(os.environ.get("TEMP", ".")) / "l_repo_sync_gui" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"fault_{datetime.datetime.now():%Y-%m-%d}.log"


def main():
    # ---- 启动阶段诊断日志 ----
    _write_runtime_log("[STARTUP] l_repo_sync_gui starting", echo=True)
    _write_runtime_log(f"[STARTUP] Python: {sys.executable}", echo=True)
    _write_runtime_log(f"[STARTUP] argv: {sys.argv}", echo=True)

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", choices=["server", "client"], default="")
    parser.add_argument(
        "--refresh-status",
        action="store_true",
        help="启动时扫描并刷新各包的 git 状态",
    )
    args, _unknown = parser.parse_known_args()
    mode_map = {"server": "Server", "client": "Client"}

    _set_windows_app_id()

    # ---- faulthandler：捕获 C++ 层 segfault 崩溃堆栈 ----
    try:
        fault_fh = _fault_log_path().open("a", encoding="utf-8")
        fault_fh.write(
            f"\n=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} "
            f"l_repo_sync_gui started (pid={os.getpid()}) ===\n"
        )
        fault_fh.flush()
        faulthandler.enable(file=fault_fh, all_threads=True)
        _write_runtime_log(
            f"[STARTUP] faulthandler enabled -> {_fault_log_path()}", echo=True
        )
    except Exception:
        _write_runtime_log(
            "[STARTUP][WARN] faulthandler init failed:\n" +
            traceback.format_exc(),
            echo=True,
        )

    def _stall_on_trigger(info):
        """卡顿回调：同时写 stderr 和日志文件（不依赖 GUI）。"""
        msg = (
            f"[stall] {info.thread_name} 卡顿了({info.duration:.1f}s) "
            f"位置: {info.stack_signature[:120]}"
        )
        # 1. stderr（即使无控制台窗口也会写到父进程/终端）
        print(msg, file=sys.stderr, flush=True)
        # 2. 日志文件（如果已初始化）
        if _stall_log_fh is not None:
            try:
                _stall_log_fh.write(msg + "\n")
                _stall_log_fh.flush()
            except Exception:
                pass

    # 先创建日志文件，供 stall 回调写入
    _stall_log_fh = None
    try:
        _stall_log_fh = _runtime_log_path().open("a", encoding="utf-8")
    except Exception:
        pass

    try:
        lprint.stall_monitor_start(
            threshold_s=5,
            poll_interval=1,
            monitor_all_threads=True,
            on_trigger=_stall_on_trigger,
        )
    except Exception:
        _write_runtime_log(
            "[WARN] stall_monitor 启动失败:\n" + traceback.format_exc())

    _write_runtime_log("[STARTUP] creating QApplication", echo=True)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if APP_ICON_FILE.exists():
        app.setWindowIcon(QIcon(str(APP_ICON_FILE)))
    launch_command = " ".join(shlex.quote(part) for part in sys.argv)

    _write_runtime_log("[STARTUP] creating RepoSyncWindow", echo=True)
    win = RepoSyncWindow(
        mode_map.get(args.mode) or None,
        refresh_status_on_start=args.refresh_status,
        launch_command=launch_command,
    )

    _write_runtime_log("[STARTUP] calling win.show()", echo=True)
    win.show()
    _write_runtime_log("[STARTUP] entering app.exec()", echo=True)
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        _write_runtime_log(
            "[FATAL] l_repo_sync_gui crashed:\n" + traceback.format_exc(), echo=True)
        raise
