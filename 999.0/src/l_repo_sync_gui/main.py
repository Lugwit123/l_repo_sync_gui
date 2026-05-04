import datetime
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
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
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"
DEFAULT_SILICONFLOW_MODEL = "Pro/zai-org/GLM-4.7"
DEFAULT_SILICONFLOW_KEY = "sk-gzwtmzfhglvibdbvrttmsuuqsyyjxghxlxzdhubdefmshqoi"


def _find_rez_source_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if parent.name == "rez-package-source":
            return parent
    raise RuntimeError("Cannot find rez-package-source from current script path.")


class _AiBridge(QObject):
    finished = Signal(bool, str)


class RepoSyncWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.rez_source = _find_rez_source_root()
        self.setWindowTitle("Rez Package Repo Sync")
        self.resize(1100, 760)

        self.owner_edit = QLineEdit("Lugwit123")
        self.owner_edit.setPlaceholderText("GitHub owner, e.g. Lugwit123")
        self.force_push_checkbox = QCheckBox("强制本地覆盖远端（--force-with-lease）")
        self.force_push_checkbox.setToolTip("仅上传时生效。会用本地提交覆盖远端分支，请谨慎使用。")
        self.api_key_edit = QLineEdit(
            os.environ.get("SILICONFLOW_API_KEY", DEFAULT_SILICONFLOW_KEY)
        )
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("SiliconFlow API Key")
        self.model_edit = QLineEdit(DEFAULT_SILICONFLOW_MODEL)
        self.model_edit.setPlaceholderText("SiliconFlow model")
        self.ai_prompt_edit = QTextEdit()
        self.ai_prompt_edit.setPlaceholderText("输入问题，例如：请总结当前上传预览的风险点")
        self.ai_prompt_edit.setFixedHeight(72)
        self.ai_answer_edit = QTextEdit()
        self.ai_answer_edit.setReadOnly(True)
        self.ai_answer_edit.setFixedHeight(140)
        self.ai_ask_btn: QPushButton | None = None
        self.ai_bridge = _AiBridge()
        self.ai_bridge.finished.connect(self._on_ai_result)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.row_buttons = []

        self._build_ui()
        self.refresh_packages()

    def _build_ui(self):
        root = QWidget(self)
        main_layout = QVBoxLayout(root)

        owner_line = QHBoxLayout()
        owner_line.addWidget(QLabel("GitHub Owner:"))
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
        main_layout.addLayout(owner_line)
        main_layout.addWidget(self.force_push_checkbox)

        body_layout = QHBoxLayout()

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.list_area = QScrollArea()
        self.list_area.setWidgetResizable(True)
        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setAlignment(Qt.AlignTop)
        self.list_area.setWidget(self.list_widget)
        left_layout.addWidget(self.list_area, 3)
        left_layout.addWidget(QLabel("日志输出:"))
        left_layout.addWidget(self.log_edit, 2)
        body_layout.addWidget(left_panel, 3)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        ai_line = QHBoxLayout()
        ai_line.addWidget(QLabel("硅基 Key:"))
        ai_line.addWidget(self.api_key_edit, 2)
        ai_line.addWidget(QLabel("模型:"))
        ai_line.addWidget(self.model_edit, 1)
        self.ai_ask_btn = QPushButton("问AI")
        self.ai_ask_btn.clicked.connect(self.ask_ai)
        ai_line.addWidget(self.ai_ask_btn)
        right_layout.addLayout(ai_line)
        right_layout.addWidget(QLabel("AI提问:"))
        right_layout.addWidget(self.ai_prompt_edit)
        right_layout.addWidget(QLabel("AI回复:"))
        right_layout.addWidget(self.ai_answer_edit, 1)
        body_layout.addWidget(right_panel, 2)

        main_layout.addLayout(body_layout, 1)

        self.setCentralWidget(root)

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
        self._log(f"$ {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            return False, str(exc)
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        merged = "\n".join(x for x in [out, err] if x)
        if result.returncode != 0:
            return False, merged or f"exit code={result.returncode}"
        return True, merged

    def _check_tools(self) -> bool:
        ok_git, msg_git = self._run(["git", "--version"])
        ok_gh, msg_gh = self._run(["gh", "--version"])
        if not ok_git:
            QMessageBox.warning(self, "缺少工具", f"找不到 git:\n{msg_git}")
            return False
        if not ok_gh:
            QMessageBox.warning(self, "缺少工具", f"找不到 gh:\n{msg_gh}")
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

    def refresh_packages(self):
        while self.list_layout.count():
            child = self.list_layout.takeAt(0)
            w = child.widget()
            if w:
                w.deleteLater()
        self.row_buttons = []

        for pkg_name, pkg_dir in self._package_entries():
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(4, 4, 4, 4)

            label = QLabel(pkg_name)
            label.setMinimumWidth(220)
            row_lay.addWidget(label)

            btn_up = QPushButton("上传")
            btn_up.clicked.connect(lambda _=False, p=pkg_dir: self.upload_one(p))
            row_lay.addWidget(btn_up)
            self.row_buttons.append(btn_up)

            btn_down = QPushButton("下载")
            btn_down.clicked.connect(lambda _=False, p=pkg_dir: self.download_one(p))
            row_lay.addWidget(btn_down)
            self.row_buttons.append(btn_down)

            self.list_layout.addWidget(row)

        self._log(f"已加载 {self.list_layout.count()} 个包。")

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

    def _clip_lines(self, lines: list[str], limit: int = PREVIEW_MAX_LINES) -> list[str]:
        clipped = [x for x in lines if x is not None]
        if len(clipped) > limit:
            return clipped[:limit] + [f"...(共 {len(clipped)} 行，仅显示前 {limit} 行)"]
        return clipped

    def _confirm_action(self, title: str, pkg_name: str, lines: list[str], fallback: str) -> bool:
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

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setIcon(QMessageBox.Question)
        msg_box.setText(f"{pkg_name} 将执行文件同步。\n\n关键文件变更（含删除）:\n{summary_text}")
        msg_box.setInformativeText("点击“显示详情”可查看每个文件改动(diff)。是否继续？")
        msg_box.setDetailedText("\n".join(clipped))
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        return msg_box.exec() == QMessageBox.Yes

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
        ok_status, status_lines = self._status_lines(pkg_dir)
        if not ok_status:
            return status_lines
        out = ["[上传预览] 本地将覆盖远端（push）", "", "[文件列表: git status --porcelain]"]
        out.extend(status_lines or ["(无本地文件变化)"])
        out.append("")
        # 显示 tracked 文件变更，便于确认“每个文件改了什么”。
        out.extend(self._diff_lines(pkg_dir, ["git", "diff", "--patch"], "工作区修改 diff"))
        out.append("")
        out.extend(self._diff_lines(pkg_dir, ["git", "diff", "--cached", "--patch"], "已暂存修改 diff"))
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
            if not self._confirm_action("确认上传", pkg_name, upload_preview, "无文件变化（可能仅推送远端分支状态）"):
                self._log(f"[info] {pkg_name} 取消上传")
                return
            branch = self._current_branch(pkg_dir)

            self._run(["git", "add", "-A"], cwd=pkg_dir)
            ok, _ = self._run(["git", "diff", "--cached", "--quiet"], cwd=pkg_dir)
            if not ok:
                ai_commit_msg = self._request_ai_commit_message(pkg_name, pkg_dir)
                commit_msg = ai_commit_msg or f"sync {pkg_name}\n\nl_repo_sync_gui automated commit"
                if ai_commit_msg:
                    self._log(f"[ok] {pkg_name} 使用 AI 生成提交注释")
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

            self._log(f"[info] push 失败，尝试 gh repo create: {msg_push}")
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
            if not self._confirm_action("确认下载", pkg_name, download_preview, "无远端文件变化"):
                self._log(f"[info] {pkg_name} 取消下载")
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
        model = self.model_edit.text().strip() or DEFAULT_SILICONFLOW_MODEL
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
                with urllib.request.urlopen(req, timeout=60) as resp:
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
            return None
        model = self.model_edit.text().strip() or DEFAULT_SILICONFLOW_MODEL

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
            with urllib.request.urlopen(req, timeout=45) as resp:
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
            self._log(f"[WARN] AI 生成提交注释失败，使用默认注释: {exc}")
            return None


def main():
    app = QApplication(sys.argv)
    win = RepoSyncWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
