import argparse
import concurrent.futures
import ctypes
import datetime
import difflib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QTextEdit,
)


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

SKIP_DIRS = {"repo_tools"}
PREVIEW_MAX_LINES = 300
# Git HTTPS：建立 TCP+TLS 连接阶段最长等待（秒），避免 SSL 握手长时间挂死
GIT_HTTP_CONNECT_TIMEOUT_SEC = 5
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"
# 国内硅基流动：直连，不走系统/环境代理（避免误走公司代理导致失败或变慢）
_SILICONFLOW_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _siliconflow_urlopen(req: urllib.request.Request, timeout: float):
    return _SILICONFLOW_OPENER.open(req, timeout=timeout)


DEFAULT_SILICONFLOW_MODEL = "Qwen/Qwen2.5-72B-Instruct"
MODEL_PRESETS = [
    "Qwen/Qwen2.5-72B-Instruct",
    "deepseek-ai/DeepSeek-V4-Flash",
    "Pro/zai-org/GLM-4.7",
]
DEFAULT_SILICONFLOW_KEY = "sk-gzwtmzfhglvibdbvrttmsuuqsyyjxghxlxzdhubdefmshqoi"
AUTH_CONFIG_FILE = Path.home() / ".l_repo_sync_gui_auth.json"
APP_ID = "lugwit.l_repo_sync_gui"
APP_ICON_FILE = Path(__file__).resolve().with_name("app_icon.svg")


def _find_rez_source_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if parent.name == "rez-package-source":
            return parent
    raise RuntimeError("Cannot find rez-package-source from current script path.")


def _set_windows_app_id():
    """设置 Windows AppUserModelID，确保任务栏/通知图标归组一致。"""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        # 非关键能力，失败时静默回退。
        pass


class _AiBridge(QObject):
    finished = Signal(bool, str)


class _AiStreamBridge(QObject):
    status = Signal(str)
    chunk = Signal(str)
    finished = Signal(bool, str)


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


class RepoSyncWindow(QMainWindow):
    def __init__(self, launch_mode: str | None = None):
        super().__init__()
        self.rez_source = _find_rez_source_root()
        title = "Rez Package Repo Sync"
        if launch_mode:
            title = f"{title} - {launch_mode}"
        self.setWindowTitle(title)
        self.resize(1280, 820)
        self.setMinimumSize(1100, 700)
        if APP_ICON_FILE.exists():
            self.setWindowIcon(QIcon(str(APP_ICON_FILE)))

        self.owner_edit = QLineEdit("Lugwit123")
        self.owner_edit.setPlaceholderText("GitHub owner, e.g. Lugwit123")
        self.git_path_edit = QLineEdit()
        self.git_path_edit.setPlaceholderText("git.exe path (optional, auto-detect if empty)")
        self.gh_path_edit = QLineEdit()
        self.gh_path_edit.setPlaceholderText("gh.exe path (optional, auto-detect if empty)")
        self.gh_token_edit = QLineEdit()
        self.gh_token_edit.setPlaceholderText("GitHub token (used by GH_TOKEN)")
        self.gh_token_edit.setEchoMode(QLineEdit.Password)
        self.force_push_checkbox = QCheckBox("强制本地覆盖远端（--force-with-lease）")
        self.force_push_checkbox.setToolTip("仅上传时生效。会用本地提交覆盖远端分支，请谨慎使用。")
        self.force_download_checkbox = QCheckBox("下载时强制覆盖本地（丢弃本地修改）")
        self.force_download_checkbox.setToolTip(
            "下载时使用 fetch + reset --hard + clean -fd，"
            "会丢弃本地未提交改动和未跟踪文件，请谨慎使用。"
        )
        self.api_key_edit = QLineEdit(
            os.environ.get("SILICONFLOW_API_KEY", DEFAULT_SILICONFLOW_KEY)
        )
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("SiliconFlow API Key")
        self.model_edit = QComboBox()
        self.model_edit.setEditable(True)
        self.model_edit.addItems(MODEL_PRESETS)
        self.model_edit.setCurrentText(DEFAULT_SILICONFLOW_MODEL)
        if self.model_edit.lineEdit():
            self.model_edit.lineEdit().setPlaceholderText("SiliconFlow model")
        self.ai_prompt_edit = QTextEdit()
        self.ai_prompt_edit.setPlaceholderText("输入问题，例如：请总结当前上传预览的风险点")
        self.ai_prompt_edit.setMinimumHeight(120)
        self.ai_answer_edit = QTextEdit()
        self.ai_answer_edit.setReadOnly(True)
        self.ai_answer_edit.setMinimumHeight(220)
        self.ai_ask_btn: QPushButton | None = None
        self.ai_bridge = _AiBridge()
        self.ai_bridge.finished.connect(self._on_ai_result)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMinimumHeight(180)
        self.row_buttons = []

        self._apply_style()
        self._build_ui()
        self.refresh_packages()

    def _apply_style(self):
        self.setStyleSheet(
            """
            QMainWindow { background: #1f1f22; }
            QLabel { font-size: 13px; }
            QPushButton { min-height: 28px; padding: 1px 8px; }
            QLineEdit, QTextEdit {
                font-size: 13px;
                border: 1px solid #3b3b3f;
                border-radius: 4px;
                padding: 6px;
            }
            """
        )
        self._row_button_style = "QPushButton { min-height: 20px; max-height: 20px; padding: 0px 4px; font-size: 12px; }"

    def _build_ui(self):
        root = QWidget(self)
        main_layout = QVBoxLayout(root)
        main_layout.setContentsMargins(12, 10, 12, 12)
        main_layout.setSpacing(10)

        top_bar = QFrame()
        top_bar.setFrameShape(QFrame.StyledPanel)
        top_lay = QVBoxLayout(top_bar)
        top_lay.setContentsMargins(10, 8, 10, 8)
        top_lay.setSpacing(8)

        owner_line = QHBoxLayout()
        owner_line.setSpacing(8)
        owner_line.addWidget(QLabel("GitHub Owner:"))
        self.owner_edit.setMinimumWidth(240)
        owner_line.addWidget(self.owner_edit, 1)

        btn_refresh = QPushButton("刷新包列表")
        btn_refresh.clicked.connect(self.refresh_packages)
        owner_line.addWidget(btn_refresh)

        btn_upload_all = QPushButton("批量上传")
        btn_upload_all.clicked.connect(self.upload_all)
        owner_line.addWidget(btn_upload_all)

        btn_download_all = QPushButton("批量下载")
        btn_download_all.clicked.connect(self.download_all)
        owner_line.addWidget(btn_download_all)

        btn_restart = QPushButton("重启")
        btn_restart.clicked.connect(self.restart_self)
        owner_line.addWidget(btn_restart)
        top_lay.addLayout(owner_line)

        gh_line = QHBoxLayout()
        gh_line.setSpacing(8)
        gh_line.addWidget(QLabel("git.exe:"))
        gh_line.addWidget(self.git_path_edit, 1)
        gh_line.addWidget(QLabel("gh.exe:"))
        gh_line.addWidget(self.gh_path_edit, 1)
        top_lay.addLayout(gh_line)

        auth_line = QHBoxLayout()
        auth_line.setSpacing(8)
        auth_line.addWidget(QLabel("GitHub Token:"))
        auth_line.addWidget(self.gh_token_edit, 1)
        btn_save_token = QPushButton("保存Token")
        btn_save_token.clicked.connect(self._save_auth_settings)
        auth_line.addWidget(btn_save_token)
        btn_clear_token = QPushButton("清除Token")
        btn_clear_token.clicked.connect(self._clear_auth_token)
        auth_line.addWidget(btn_clear_token)
        btn_auth_status = QPushButton("检查授权")
        btn_auth_status.clicked.connect(self._check_github_auth)
        auth_line.addWidget(btn_auth_status)
        btn_gh_login = QPushButton("网页登录授权")
        btn_gh_login.clicked.connect(self._start_gh_auth_login)
        auth_line.addWidget(btn_gh_login)
        top_lay.addLayout(auth_line)

        top_lay.addWidget(self.force_push_checkbox)
        top_lay.addWidget(self.force_download_checkbox)
        main_layout.addWidget(top_bar)

        body_splitter = QSplitter(Qt.Horizontal)
        body_splitter.setChildrenCollapsible(False)

        left_panel = QSplitter(Qt.Vertical)
        left_panel.setChildrenCollapsible(False)

        pkg_panel = QWidget()
        left_layout = QVBoxLayout(pkg_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_layout.addWidget(QLabel("包列表:"))
        self.list_area = QScrollArea()
        self.list_area.setWidgetResizable(True)
        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(1)
        self.list_layout.setAlignment(Qt.AlignTop)
        self.list_area.setWidget(self.list_widget)
        left_layout.addWidget(self.list_area, 1)

        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(6)
        log_layout.addWidget(QLabel("日志输出:"))
        log_layout.addWidget(self.log_edit, 1)

        left_panel.addWidget(pkg_panel)
        left_panel.addWidget(log_panel)
        left_panel.setStretchFactor(0, 3)
        left_panel.setStretchFactor(1, 2)
        left_panel.setSizes([430, 260])

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        ai_line = QHBoxLayout()
        ai_line.setSpacing(8)
        ai_line.addWidget(QLabel("硅基 Key:"))
        ai_line.addWidget(self.api_key_edit, 2)
        ai_line.addWidget(QLabel("模型:"))
        ai_line.addWidget(self.model_edit, 1)
        btn_model_test = QPushButton("测试模型连接")
        btn_model_test.clicked.connect(self._test_model_connection)
        ai_line.addWidget(btn_model_test)
        self.ai_ask_btn = QPushButton("问AI")
        self.ai_ask_btn.clicked.connect(self.ask_ai)
        ai_line.addWidget(self.ai_ask_btn)
        right_layout.addLayout(ai_line)
        right_layout.addWidget(QLabel("AI提问:"))
        right_layout.addWidget(self.ai_prompt_edit)
        right_layout.addWidget(QLabel("AI回复:"))
        right_layout.addWidget(self.ai_answer_edit, 1)
        body_splitter.addWidget(left_panel)
        body_splitter.addWidget(right_panel)
        body_splitter.setStretchFactor(0, 3)
        body_splitter.setStretchFactor(1, 2)
        body_splitter.setSizes([760, 500])

        main_layout.addWidget(body_splitter, 1)

        self.setCentralWidget(root)
        self._init_gh_path()
        self._load_auth_settings()

    def _init_gh_path(self):
        """初始化 git/gh 路径输入框：优先环境变量，其次自动探测。"""
        env_git = (os.environ.get("GIT_EXE_PATH") or "").strip()
        if env_git:
            self.git_path_edit.setText(env_git)
        else:
            auto_git = self._resolve_git_executable(from_manual=False)
            if auto_git:
                self.git_path_edit.setText(auto_git)

        env_gh = (os.environ.get("GH_EXE_PATH") or "").strip()
        if env_gh:
            self.gh_path_edit.setText(env_gh)
            return
        auto = self._resolve_gh_executable(from_manual=False)
        if auto:
            self.gh_path_edit.setText(auto)

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

    def _check_github_auth(self):
        ok, msg = self._run(["gh", "auth", "status"])
        if ok:
            QMessageBox.information(self, "GitHub 授权状态", msg or "已授权。")
        else:
            QMessageBox.warning(self, "GitHub 授权状态", msg or "未授权。")

    def _start_gh_auth_login(self):
        gh_exe = self._resolve_gh_executable()
        if not gh_exe:
            QMessageBox.warning(self, "启动失败", "未找到 gh.exe，请先设置路径。")
            return
        try:
            subprocess.Popen(
                [
                    gh_exe,
                    "auth",
                    "login",
                    "--web",
                    "--clipboard",
                    "--hostname",
                    "github.com",
                    "--git-protocol",
                    "https",
                ],
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010),
            )
            self._log("[info] 已启动网页登录授权（已复制授权码到剪贴板并打开网页）。")
        except Exception as exc:
            self._log(f"[ERR] 启动 gh auth login 失败: {exc}")
            QMessageBox.warning(self, "启动失败", str(exc))

    def _resolve_git_executable(self, from_manual: bool = True) -> str | None:
        """返回可执行 git 路径（优先用户输入）。"""
        manual = (self.git_path_edit.text() or "").strip() if from_manual else ""
        if manual:
            if os.path.isfile(manual):
                return manual
            return None

        which_git = shutil.which("git")
        if which_git and os.path.isfile(which_git):
            return which_git

        default_paths = [
            r"C:\Program Files\Git\cmd\git.exe",
            r"C:\Program Files\Git\bin\git.exe",
            r"C:\Program Files (x86)\Git\cmd\git.exe",
            r"C:\Program Files (x86)\Git\bin\git.exe",
        ]
        for p in default_paths:
            if os.path.isfile(p):
                return p
        return None

    def _resolve_gh_executable(self, from_manual: bool = True) -> str | None:
        """返回可执行 gh 路径（优先用户输入）。"""
        manual = (self.gh_path_edit.text() or "").strip() if from_manual else ""
        if manual:
            if os.path.isfile(manual):
                return manual
            return None

        which_gh = shutil.which("gh")
        if which_gh and os.path.isfile(which_gh):
            return which_gh

        default_paths = [
            r"C:\Program Files\GitHub CLI\gh.exe",
            r"C:\Program Files (x86)\GitHub CLI\gh.exe",
        ]
        for p in default_paths:
            if os.path.isfile(p):
                return p
        return None

    def _set_busy(self, busy: bool):
        for btn in self.row_buttons:
            btn.setEnabled(not busy)
        QApplication.processEvents()

    def _log(self, text: str):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_edit.append(f"[{now}] {text}")
        self.log_edit.ensureCursorVisible()
        QApplication.processEvents()

    def _run(self, cmd: list[str], cwd: Path | None = None) -> tuple[bool, str]:
        if not cmd:
            return False, "empty command"

        effective_cmd = list(cmd)
        if cmd[0] == "git":
            git_exe = self._resolve_git_executable()
            if not git_exe:
                return False, "git.exe not found"
            # 不修改全局 gitconfig，仅本次进程内生效
            effective_cmd = [
                git_exe,
                "-c",
                f"http.connectTimeout={GIT_HTTP_CONNECT_TIMEOUT_SEC}",
                *cmd[1:],
            ]
        elif cmd[0] == "gh":
            gh_exe = self._resolve_gh_executable()
            if not gh_exe:
                return False, "gh.exe not found"
            effective_cmd[0] = gh_exe

        self._log(f"$ {' '.join(effective_cmd)}")
        run_env = os.environ.copy()
        if cmd[0] == "git":
            run_env["GIT_HTTP_CONNECT_TIMEOUT"] = str(GIT_HTTP_CONNECT_TIMEOUT_SEC)
        gh_token = (self.gh_token_edit.text() or "").strip()
        if gh_token:
            run_env["GH_TOKEN"] = gh_token
            run_env["GITHUB_TOKEN"] = gh_token
        try:
            proc = subprocess.Popen(
                effective_cmd,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=run_env,
            )
            while True:
                try:
                    out_b, err_b = proc.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    QApplication.processEvents()
        except Exception as exc:
            return False, str(exc)

        def _decode_output(data: bytes | None) -> str:
            if not data:
                return ""
            for enc in ("utf-8", "gbk", sys.getdefaultencoding()):
                try:
                    return data.decode(enc)
                except Exception:
                    continue
            return data.decode("utf-8", errors="replace")

        out = _decode_output(out_b).strip()
        err = _decode_output(err_b).strip()
        merged = "\n".join(x for x in [out, err] if x)
        if proc.returncode != 0:
            return False, merged or f"exit code={proc.returncode}"
        return True, merged

    def _check_tools(self) -> bool:
        ok_git, msg_git = self._run(["git", "--version"])
        ok_gh, msg_gh = self._run(["gh", "--version"])
        if not ok_git:
            QMessageBox.warning(
                self,
                "缺少工具",
                (
                    "找不到 git。\n"
                    "请在顶部 git.exe 输入框设置路径，"
                    "例如: C:\\Program Files\\Git\\cmd\\git.exe\n\n"
                    f"详细信息:\n{msg_git}"
                ),
            )
            return False
        if not ok_gh:
            QMessageBox.warning(
                self,
                "缺少工具",
                (
                    "找不到 gh。\n"
                    "请在顶部 gh.exe 输入框设置路径，"
                    "例如: C:\\Program Files\\GitHub CLI\\gh.exe\n\n"
                    f"详细信息:\n{msg_gh}"
                ),
            )
            return False
        return True

    def _package_entries(self) -> list[tuple[str, Path]]:
        out: list[tuple[str, Path]] = []
        for item in sorted(self.rez_source.iterdir(), key=lambda p: p.name.lower()):
            if not item.is_dir():
                continue
            if item.name in SKIP_DIRS:
                continue
            if not list(item.glob("*/package.py")):
                continue
            out.append((item.name, item))

        # 与 repo_tools/batch_push_github.bat 一致：额外处理 trayapp/wuwo 仓库。
        wuwo_dir = self.rez_source.parent / "wuwo"
        if wuwo_dir.is_dir():
            out.append(("wuwo", wuwo_dir))
        return out

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

    def _scan_package_status(self, pkg_dir: Path) -> tuple[bool, dict[str, int], str]:
        """扫描单个包状态（给包列表统计使用，不写日志）。"""
        if not (pkg_dir / ".git").is_dir():
            return False, {}, "非git仓库"
        git_exe = self._resolve_git_executable()
        if not git_exe:
            return False, {}, "未找到git.exe"
        try:
            proc = subprocess.run(
                [git_exe, "status", "--porcelain"],
                cwd=str(pkg_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except Exception as exc:
            return False, {}, f"扫描失败: {exc}"

        if proc.returncode != 0:
            err = (proc.stderr or "").strip() or f"exit code={proc.returncode}"
            return False, {}, f"扫描失败: {err}"

        status_lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        return True, self._summarize_status_counts(status_lines), ""

    @staticmethod
    def _format_package_status_suffix(counts: dict[str, int], err: str) -> str:
        if err:
            return f" [{err}]"
        if not counts:
            return ""
        parts: list[str] = []
        if counts.get("modified", 0):
            parts.append(f"修改{counts['modified']}")
        if counts.get("staged", 0):
            parts.append(f"暂存{counts['staged']}")
        if counts.get("untracked", 0):
            parts.append(f"未跟踪{counts['untracked']}")
        if counts.get("deleted", 0):
            parts.append(f"删除{counts['deleted']}")
        if counts.get("renamed", 0):
            parts.append(f"重命名{counts['renamed']}")
        if counts.get("conflicted", 0):
            parts.append(f"冲突{counts['conflicted']}")
        if not parts:
            return " [干净]"
        return f" [{' / '.join(parts)}]"

    def refresh_packages(self):
        while self.list_layout.count():
            child = self.list_layout.takeAt(0)
            w = child.widget()
            if w:
                w.deleteLater()
        self.row_buttons = []
        package_entries = self._package_entries()
        status_map: dict[str, tuple[dict[str, int], str]] = {}
        max_workers = max(1, min(8, (os.cpu_count() or 4), len(package_entries) or 1))
        if package_entries:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(self._scan_package_status, pkg_dir): pkg_name
                    for pkg_name, pkg_dir in package_entries
                }
                for future in concurrent.futures.as_completed(futures):
                    pkg_name = futures[future]
                    try:
                        ok, counts, err = future.result()
                    except Exception as exc:
                        ok, counts, err = False, {}, f"扫描失败: {exc}"
                    status_map[pkg_name] = (counts if ok else {}, "" if ok else err)
                    QApplication.processEvents()

        for pkg_name, pkg_dir in package_entries:
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(1, 0, 1, 0)
            row_lay.setSpacing(4)

            counts, err = status_map.get(pkg_name, ({}, ""))
            label = QLabel(f"{pkg_name}{self._format_package_status_suffix(counts, err)}")
            label.setStyleSheet("font-size: 12px;")
            label.setMinimumWidth(260)
            row_lay.addWidget(label)

            btn_up = QPushButton("上传")
            btn_up.setStyleSheet(self._row_button_style)
            btn_up.setMinimumHeight(20)
            btn_up.setMaximumWidth(72)
            btn_up.clicked.connect(lambda _=False, p=pkg_dir: self.upload_one(p))
            row_lay.addWidget(btn_up)
            self.row_buttons.append(btn_up)

            btn_down = QPushButton("下载")
            btn_down.setStyleSheet(self._row_button_style)
            btn_down.setMinimumHeight(20)
            btn_down.setMaximumWidth(72)
            btn_down.clicked.connect(lambda _=False, p=pkg_dir: self.download_one(p))
            row_lay.addWidget(btn_down)
            self.row_buttons.append(btn_down)

            self.list_layout.addWidget(row)
            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setFrameShadow(QFrame.Plain)
            sep.setFixedHeight(2)
            sep.setStyleSheet("color: #5a5a62; margin: 0px; padding: 0px;")
            self.list_layout.addWidget(sep)

        self._log(f"已加载 {len(package_entries)} 个包（状态已并行扫描）。")

    def _ensure_git_repo(self, pkg_dir: Path) -> bool:
        if (pkg_dir / ".git").is_dir():
            return True
        ok, msg = self._run(["git", "init", "-b", "main"], cwd=pkg_dir)
        if not ok:
            self._log(f"[ERR] {pkg_dir.name} git init 失败: {msg}")
            return False
        self._run(["git", "config", "core.longpaths", "true"], cwd=pkg_dir)
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
        for line in IGNORE_LINES:
            if line not in existing:
                lines.append(line)
                changed = True
        if changed:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _ensure_remote(self, pkg_dir: Path, pkg_name: str, owner: str):
        remote_url = f"https://github.com/{owner}/{pkg_name}.git"
        ok, _ = self._run(["git", "remote", "get-url", "origin"], cwd=pkg_dir)
        if ok:
            self._run(["git", "remote", "set-url", "origin", remote_url], cwd=pkg_dir)
        else:
            self._run(["git", "remote", "add", "origin", remote_url], cwd=pkg_dir)

    def _current_branch(self, pkg_dir: Path) -> str:
        ok, msg = self._run(["git", "branch", "--show-current"], cwd=pkg_dir)
        branch = (msg or "").strip().splitlines()[-1] if ok and msg.strip() else "main"
        return branch or "main"

    @staticmethod
    def _should_try_create_repo(push_err: str) -> bool:
        """仅在明显“远端仓库不存在”时才尝试 gh repo create。"""
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
        if any(m in low for m in network_markers):
            return False
        if any(m in low for m in reject_markers):
            return False
        return any(m in low for m in repo_not_found_markers)

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

    def _clip_lines(self, lines: list[str], limit: int = PREVIEW_MAX_LINES) -> list[str]:
        clipped = [x for x in lines if x is not None]
        if len(clipped) > limit:
            return clipped[:limit] + [f"...(共 {len(clipped)} 行，仅显示前 {limit} 行)"]
        return clipped

    def _confirm_action(
        self, title: str, pkg_name: str, lines: list[str], fallback: str, enable_ai: bool = False
    ) -> tuple[bool, str | None]:
        preview = [x for x in lines if x.strip()]
        if not preview:
            preview = [fallback]
        clipped = self._clip_lines(preview)

        summary = []
        for line in clipped:
            if line.startswith(" A ") or line.startswith("A "):
                summary.append(line)
            elif line.startswith(" M ") or line.startswith("M "):
                summary.append(line)
            elif line.startswith(" D ") or line.startswith("D "):
                summary.append(line)
        summary = summary[:25]
        summary_text = "\n".join(summary) if summary else "(见详细变更)"

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(980, 720)
        dlg.setMinimumSize(860, 620)
        layout = QVBoxLayout(dlg)

        info = QLabel(
            f"{pkg_name} 将执行文件同步。"
            "\n下方按标签页显示完整预览（含完整路径与 diff）。是否继续？"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        summary_box = QTextEdit(dlg)
        summary_box.setReadOnly(True)
        summary_box.setPlainText(f"关键文件变更（新增/修改/删除）:\n{summary_text}")
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

        file_blocks = self._extract_file_diff_blocks(clipped)
        for file_name, block in file_blocks:
            editor = QTextEdit(dlg)
            editor.setReadOnly(True)
            editor.setPlainText(block)
            self._apply_diff_highlight(editor)
            tabs.addTab(editor, self._safe_tab_name(file_name))

        main_content_splitter = QSplitter(Qt.Horizontal, dlg)
        main_content_splitter.setChildrenCollapsible(False)
        main_content_splitter.addWidget(tabs)

        ai_detail_edit: QTextEdit | None = None
        ai_use_as_commit = None
        dialog_force_push = None
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

            dialog_force_push = QCheckBox("本次上传使用强制推送（--force-with-lease）")
            dialog_force_push.setChecked(self.force_push_checkbox.isChecked())
            ai_panel_lay.addWidget(dialog_force_push)

            stream_bridge = _AiStreamBridge()
            current_ai_text = {"text": ""}

            def _append_status(msg: str):
                ai_trace_edit.append(msg)
                ai_trace_edit.ensureCursorVisible()

            def _append_chunk(chunk: str):
                if not chunk:
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

            def _run_ai_review():
                ai_btn.setEnabled(False)
                ai_btn.setText("分析中...")
                ai_trace_edit.clear()
                current_ai_text["text"] = ""
                ai_detail_edit.clear()

                ai_input_lines = self._clip_lines(clipped, limit=1200)
                prompt = self._build_ai_detailed_review_prompt(pkg_name, "\n".join(ai_input_lines))
                ai_input_edit.setPlainText(prompt)
                in_token_label.setText(f"输入Token(估算): {self._estimate_token_count(prompt)}")
                out_token_label.setText("输出Token(估算): 0")
                stream_bridge.status.emit("[INFO] 已构建输入，开始请求 AI（stream）。")

                def _worker():
                    ok, msg = self._stream_ai_detailed_review(
                        prompt=prompt,
                        on_status=lambda s: stream_bridge.status.emit(s),
                        on_chunk=lambda c: stream_bridge.chunk.emit(c),
                    )
                    stream_bridge.finished.emit(ok, msg)

                threading.Thread(target=_worker, daemon=True).start()

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
        accepted = dlg.exec() == QDialog.Accepted
        if accepted and enable_ai and dialog_force_push is not None:
            # 与主界面保持一致，便于后续 push 逻辑复用现有开关。
            self.force_push_checkbox.setChecked(dialog_force_push.isChecked())
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
        return "..." + file_name[-(max_len - 3) :]

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
                abs_path = line.split("] ", 1)[1].strip() if "] " in line else line
                current_name = Path(abs_path).name or f"untracked_{len(blocks) + 1}"
                current_lines = [line]
                continue
            if line.startswith("diff --git "):
                if current_name and current_lines:
                    blocks.append((current_name, "\n".join(current_lines)))
                current_lines = [line]
                parts = line.split()
                if len(parts) >= 4 and parts[2].startswith("a/"):
                    current_name = parts[2][2:]
                else:
                    current_name = f"file_{len(blocks) + 1}"
                continue
            if current_name:
                current_lines.append(line)

        if current_name and current_lines:
            blocks.append((current_name, "\n".join(current_lines)))
        return blocks

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
        api_key = self.api_key_edit.text().strip()
        if not api_key:
            return False, "未填写 AI Key，无法生成详细分析"
        model = self.model_edit.currentText().strip() or DEFAULT_SILICONFLOW_MODEL
        payload = {
            "model": model,
            "stream": True,
            "messages": [
                {"role": "system", "content": "你是资深代码审查助手，擅长撰写高质量提交注释。"},
                {"role": "user", "content": prompt},
            ],
        }
        req = urllib.request.Request(
            SILICONFLOW_URL,
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
        ok, msg = self._run(["git", "status", "--porcelain"], cwd=pkg_dir)
        if not ok:
            return False, [f"[无法获取 status] {msg}"]
        lines = [line for line in (msg or "").splitlines() if line.strip()]
        return True, lines

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
        self._run(["git", "fetch", "--all", "--prune"], cwd=pkg_dir)
        ok_up, upstream = self._run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=pkg_dir
        )
        out: list[str] = []
        ok_status, status_lines = self._status_lines(pkg_dir)
        local_pending = self._format_status_lines_with_full_path(pkg_dir, status_lines) if ok_status else []

        if not ok_up or not upstream.strip():
            out.append("[远端差异预览] 无上游分支，推送时将按当前分支创建/关联远端。")
            out.append("")
            out.append("[本地未提交改动]")
            out.extend(local_pending or ["(无本地未提交改动)"])
            local_diff = self._local_uncommitted_diff_lines(pkg_dir, status_lines if ok_status else [])
            if local_diff:
                out.append("")
                out.append("[本地未提交改动 diff]")
                out.extend(local_diff)
            return out

        upstream_ref = upstream.strip().splitlines()[-1]
        out.extend([f"[远端差异预览] 本地 HEAD -> 远端 {upstream_ref}", "", "[文件列表: name-status]"])

        ok_name, msg_name = self._run(
            ["git", "diff", "--name-status", f"{upstream_ref}..HEAD"], cwd=pkg_dir
        )
        if not ok_name:
            out.append(f"[远端差异预览] 获取失败: {msg_name}")
            out.append("")
            out.append("[本地未提交改动]")
            out.extend(local_pending or ["(无本地未提交改动)"])
            local_diff = self._local_uncommitted_diff_lines(pkg_dir, status_lines if ok_status else [])
            if local_diff:
                out.append("")
                out.append("[本地未提交改动 diff]")
                out.extend(local_diff)
            return out

        lines = [line for line in (msg_name or "").splitlines() if line.strip()]
        out.extend(lines or ["(当前无已提交差异可推送)"])
        out.append("")
        out.extend(
            self._diff_lines(
                pkg_dir,
                ["git", "diff", "--patch", f"{upstream_ref}..HEAD"],
                "将推送到远端的提交差异 diff",
            )
        )
        out.append("")
        out.append("[本地未提交改动]")
        out.extend(local_pending or ["(无本地未提交改动)"])
        local_diff = self._local_uncommitted_diff_lines(pkg_dir, status_lines if ok_status else [])
        if local_diff:
            out.append("")
            out.append("[本地未提交改动 diff]")
            out.extend(local_diff)
        return out

    def _format_status_lines_with_full_path(self, pkg_dir: Path, status_lines: list[str]) -> list[str]:
        """将 git status --porcelain 输出转换为带完整路径的可读行。"""
        out: list[str] = []
        for raw in status_lines:
            line = raw.rstrip()
            if not line:
                continue
            status = line[:2].strip() or "?"
            path_part = line[3:] if len(line) > 3 else ""

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
        ok_unstaged, msg_unstaged = self._run(["git", "diff", "--patch"], cwd=pkg_dir)
        if ok_unstaged and (msg_unstaged or "").strip():
            out.extend(["[未暂存改动 diff]", msg_unstaged.strip(), ""])

        ok_staged, msg_staged = self._run(["git", "diff", "--cached", "--patch"], cwd=pkg_dir)
        if ok_staged and (msg_staged or "").strip():
            out.extend(["[已暂存改动 diff]", msg_staged.strip(), ""])

        for raw in status_lines:
            line = (raw or "").rstrip()
            if not line.startswith("?? "):
                continue
            rel = line[3:].strip()
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
        lines = [line for line in (msg_diff or "").splitlines() if line.strip()]
        out = [f"[下载预览] 远端 {upstream_ref} -> 本地 HEAD", "", "[文件列表: name-status]"]
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

    def upload_one(self, pkg_dir: Path):
        owner = self.owner_edit.text().strip() or "Lugwit123"
        pkg_name = pkg_dir.name

        if not self._check_tools():
            return
        self._set_busy(True)
        try:
            self._log(f"========== 上传 {pkg_name} ==========")
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

            self._run(["git", "add", "-A"], cwd=pkg_dir)
            ok, _ = self._run(["git", "diff", "--cached", "--quiet"], cwd=pkg_dir)
            if not ok:
                ai_commit_msg = ai_commit_message or self._request_ai_commit_message(pkg_name, pkg_dir)
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
                    self._log(f"[ok] {pkg_name} 使用 AI 生成提交注释")
                commit_msg = commit_msg or ""
                ok_commit, msg_commit = self._run(
                    ["git", "commit", "-m", commit_msg], cwd=pkg_dir
                )
                if not ok_commit:
                    self._log(f"[WARN] {pkg_name} commit 失败: {msg_commit}")
            else:
                self._log(f"[info] {pkg_name} no changes to commit")

            push_cmd = ["git", "push", "-u", "origin", branch]
            if self.force_push_checkbox.isChecked():
                confirm = QMessageBox.question(
                    self,
                    "二次确认强制覆盖",
                    (
                        f"{pkg_name} 将执行强制推送：\n"
                        f"git push -u origin {branch} --force-with-lease\n\n"
                        "这会用本地提交覆盖远端同分支历史，是否继续？"
                    ),
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if confirm != QMessageBox.Yes:
                    self._log(f"[info] {pkg_name} 取消强制上传")
                    return
                push_cmd.append("--force-with-lease")
                self._log(f"[warn] {pkg_name} 已启用强制覆盖远端")

            ok_push, msg_push = self._run(push_cmd, cwd=pkg_dir)
            if ok_push:
                self._log(f"[ok] {pkg_name} pushed")
                return

            if not self.force_push_checkbox.isChecked() and self._is_non_fast_forward_error(msg_push):
                self._log(f"[warn] {pkg_name} push 被远端拒绝，尝试自动 pull --rebase 后重试")
                ok_pull_rebase, msg_pull_rebase = self._run(
                    ["git", "pull", "--rebase", "--autostash", "origin", branch], cwd=pkg_dir
                )
                if ok_pull_rebase:
                    self._log(f"[ok] {pkg_name} pull --rebase 成功，开始重试 push")
                    ok_push_retry, msg_push_retry = self._run(push_cmd, cwd=pkg_dir)
                    if ok_push_retry:
                        self._log(f"[ok] {pkg_name} pushed（自动重试成功）")
                        return
                    self._log(f"[ERR] {pkg_name} 重试 push 失败: {msg_push_retry}")
                    QMessageBox.warning(
                        self,
                        "上传失败",
                        (
                            f"{pkg_name} push 被远端拒绝，已自动 pull --rebase 并重试，但仍失败。\n\n"
                            f"pull --rebase 输出:\n{msg_pull_rebase}\n\n"
                            f"push 重试输出:\n{msg_push_retry}"
                        ),
                    )
                    return
                self._log(f"[ERR] {pkg_name} 自动 pull --rebase 失败: {msg_pull_rebase}")
                QMessageBox.warning(
                    self,
                    "上传失败",
                    (
                        f"{pkg_name} push 被远端拒绝（non-fast-forward），自动 pull --rebase 失败。\n"
                        "请先处理 rebase 冲突或手动同步后再推送。\n\n"
                        f"详情:\n{msg_pull_rebase}"
                    ),
                )
                return

            if not self._should_try_create_repo(msg_push):
                self._log(f"[ERR] {pkg_name} push 失败（不满足自动建仓条件）: {msg_push}")
                QMessageBox.warning(
                    self,
                    "上传失败",
                    (
                        f"{pkg_name} push 失败。\n\n"
                        "本次错误不是“仓库不存在”，已跳过自动 gh repo create。\n"
                        "请检查网络/远端状态后重试。\n\n"
                        f"详情:\n{msg_push}"
                    ),
                )
                return

            self._log(f"[info] push 失败，检测为仓库不存在，尝试 gh repo create: {msg_push}")
            if not self._resolve_gh_executable():
                self._log("[ERR] 未找到 gh.exe，无法自动创建远端仓库")
                QMessageBox.warning(
                    self,
                    "上传失败",
                    (
                        f"{pkg_name} push 失败，且未找到 gh.exe。\n"
                        "请在顶部 gh.exe 输入框设置 gh 路径后重试。"
                    ),
                )
                return
            ok_create, msg_create = self._run(
                [
                    "gh",
                    "repo",
                    "create",
                    f"{owner}/{pkg_name}",
                    "--public",
                    "--source",
                    ".",
                    "--remote",
                    "origin",
                    "--push",
                ],
                cwd=pkg_dir,
            )
            if ok_create:
                self._log(f"[ok] {pkg_name} created and pushed")
            else:
                self._log(f"[ERR] {pkg_name} 上传失败: {msg_create}")
                QMessageBox.warning(self, "上传失败", f"{pkg_name} 上传失败:\n{msg_create}")
        finally:
            self._set_busy(False)

    def download_one(self, pkg_dir: Path):
        owner = self.owner_edit.text().strip() or "Lugwit123"
        pkg_name = pkg_dir.name
        if not self._check_tools():
            return
        self._set_busy(True)
        try:
            self._log(f"========== 下载 {pkg_name} ==========")
            if not pkg_dir.exists():
                pkg_dir.parent.mkdir(parents=True, exist_ok=True)
                ok_clone, msg_clone = self._run(
                    ["git", "clone", f"https://github.com/{owner}/{pkg_name}.git", str(pkg_dir)]
                )
                if ok_clone:
                    self._log(f"[ok] {pkg_name} cloned")
                else:
                    self._log(f"[ERR] {pkg_name} clone 失败: {msg_clone}")
                    QMessageBox.warning(self, "下载失败", f"{pkg_name} clone 失败:\n{msg_clone}")
                return

            if not (pkg_dir / ".git").is_dir():
                QMessageBox.warning(self, "下载失败", f"{pkg_name} 目录存在但不是 git 仓库。")
                self._log(f"[ERR] {pkg_name} 目录存在但不是 git 仓库")
                return

            download_preview = self._preview_download_files(pkg_dir)
            confirmed, _ = self._confirm_action(
                "确认下载", pkg_name, download_preview, "无远端文件变化", enable_ai=False
            )
            if not confirmed:
                self._log(f"[info] {pkg_name} 取消下载")
                return
            if self.force_download_checkbox.isChecked():
                confirm = QMessageBox.question(
                    self,
                    "二次确认强制下载",
                    (
                        f"{pkg_name} 将强制覆盖本地工作区：\n"
                        "1) git fetch --all --prune\n"
                        "2) git reset --hard <upstream>\n"
                        "3) git clean -fd\n\n"
                        "这会丢弃本地未提交改动和未跟踪文件，是否继续？"
                    ),
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if confirm != QMessageBox.Yes:
                    self._log(f"[info] {pkg_name} 取消强制下载")
                    return

                ok_fetch, msg_fetch = self._run(["git", "fetch", "--all", "--prune"], cwd=pkg_dir)
                if not ok_fetch:
                    self._log(f"[ERR] {pkg_name} fetch 失败: {msg_fetch}")
                    QMessageBox.warning(self, "下载失败", f"{pkg_name} fetch 失败:\n{msg_fetch}")
                    return

                ok_up, upstream = self._run(
                    ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=pkg_dir
                )
                if ok_up and upstream.strip():
                    target_ref = upstream.strip().splitlines()[-1]
                else:
                    target_ref = f"origin/{self._current_branch(pkg_dir)}"
                    self._log(f"[warn] {pkg_name} 无上游分支，回退使用 {target_ref}")

                ok_reset, msg_reset = self._run(["git", "reset", "--hard", target_ref], cwd=pkg_dir)
                if not ok_reset:
                    self._log(f"[ERR] {pkg_name} reset --hard 失败: {msg_reset}")
                    QMessageBox.warning(
                        self,
                        "下载失败",
                        f"{pkg_name} reset --hard {target_ref} 失败:\n{msg_reset}",
                    )
                    return

                ok_clean, msg_clean = self._run(["git", "clean", "-fd"], cwd=pkg_dir)
                if not ok_clean:
                    self._log(f"[ERR] {pkg_name} clean -fd 失败: {msg_clean}")
                    QMessageBox.warning(self, "下载失败", f"{pkg_name} clean -fd 失败:\n{msg_clean}")
                    return

                self._log(f"[ok] {pkg_name} 强制下载完成（已覆盖本地）")
                return

            ok_pull, msg_pull = self._run(["git", "pull", "--ff-only"], cwd=pkg_dir)
            if ok_pull:
                self._log(f"[ok] {pkg_name} pulled")
            else:
                self._log(f"[ERR] {pkg_name} pull 失败: {msg_pull}")
                QMessageBox.warning(self, "下载失败", f"{pkg_name} pull 失败:\n{msg_pull}")
        finally:
            self._set_busy(False)

    def upload_all(self):
        if not self._check_tools():
            return
        for _, pkg_dir in self._package_entries():
            self.upload_one(pkg_dir)

    def download_all(self):
        if not self._check_tools():
            return
        for _, pkg_dir in self._package_entries():
            self.download_one(pkg_dir)

    def restart_self(self):
        try:
            subprocess.Popen([sys.executable, *sys.argv])
            self._log("[ok] 正在重启 l_repo_sync_gui ...")
            QApplication.quit()
        except Exception as exc:
            self._log(f"[ERR] 重启失败: {exc}")
            QMessageBox.warning(self, "重启失败", str(exc))

    def ask_ai(self):
        api_key = self.api_key_edit.text().strip()
        model = self.model_edit.currentText().strip() or DEFAULT_SILICONFLOW_MODEL
        user_prompt = self.ai_prompt_edit.toPlainText().strip()
        if not api_key:
            QMessageBox.warning(self, "问AI失败", "请先填写 SiliconFlow API Key。")
            return
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
                SILICONFLOW_URL,
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
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
                if not content:
                    content = f"[接口返回为空]\n{text}"
                self.ai_bridge.finished.emit(True, content)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
                self.ai_bridge.finished.emit(False, f"HTTPError {e.code}: {body}")
            except Exception as e:
                self.ai_bridge.finished.emit(False, repr(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _test_model_connection(self):
        api_key = self.api_key_edit.text().strip()
        model = self.model_edit.currentText().strip() or DEFAULT_SILICONFLOW_MODEL
        if not api_key:
            QMessageBox.warning(self, "模型测试失败", "请先填写 SiliconFlow API Key。")
            return

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 16,
        }
        req = urllib.request.Request(
            SILICONFLOW_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with _siliconflow_urlopen(req, 5) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            data = json.loads(text)
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            self._log(f"[ok] 模型连接测试成功: {model}")
            QMessageBox.information(
                self,
                "模型连接测试",
                f"模型可用: {model}\n返回: {content or '(empty)'}",
            )
        except Exception as exc:
            self._log(f"[ERR] 模型连接测试失败: {model} -> {exc}")
            QMessageBox.warning(
                self,
                "模型连接测试失败",
                f"模型: {model}\n错误: {exc}",
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
        api_key = self.api_key_edit.text().strip()
        if not api_key:
            self._log(f"[WARN] {pkg_name} 未填写 AI Key，将改为手动输入提交注释")
            return None
        model = self.model_edit.currentText().strip() or DEFAULT_SILICONFLOW_MODEL

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

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个有用的助手，擅长写清晰的 git 提交信息。"},
                {"role": "user", "content": prompt},
            ],
        }
        req = urllib.request.Request(
            SILICONFLOW_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with _siliconflow_urlopen(req, 45) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            data = json.loads(text)
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            if not content:
                return None
            return content
        except Exception as exc:
            self._log(f"[WARN] AI 生成提交注释失败，将改为手动输入: {exc}")
            return None


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", choices=["server", "client"], default="")
    args, _unknown = parser.parse_known_args()
    mode_map = {"server": "Server", "client": "Client"}

    _set_windows_app_id()
    app = QApplication(sys.argv)
    if APP_ICON_FILE.exists():
        app.setWindowIcon(QIcon(str(APP_ICON_FILE)))
    win = RepoSyncWindow(mode_map.get(args.mode) or None)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
