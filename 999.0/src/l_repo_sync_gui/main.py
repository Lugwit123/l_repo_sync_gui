import datetime
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
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


def _find_rez_source_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if parent.name == "rez-package-source":
            return parent
    raise RuntimeError("Cannot find rez-package-source from current script path.")


class RepoSyncWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.rez_source = _find_rez_source_root()
        self.setWindowTitle("Rez Package Repo Sync")
        self.resize(1100, 760)

        self.owner_edit = QLineEdit("Lugwit123")
        self.owner_edit.setPlaceholderText("GitHub owner, e.g. Lugwit123")
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

        self.list_area = QScrollArea()
        self.list_area.setWidgetResizable(True)
        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setAlignment(Qt.AlignTop)
        self.list_area.setWidget(self.list_widget)
        main_layout.addWidget(self.list_area, 1)

        main_layout.addWidget(QLabel("日志输出:"))
        main_layout.addWidget(self.log_edit, 1)

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

    def _confirm_action(self, title: str, pkg_name: str, lines: list[str], fallback: str) -> bool:
        preview = [x for x in lines if x.strip()]
        if not preview:
            preview = [fallback]
        limit = 120
        if len(preview) > limit:
            preview = preview[:limit] + [f"...(共 {len(lines)} 条，仅显示前 {limit} 条)"]
        text = f"{pkg_name} 将执行以下文件操作：\n\n" + "\n".join(preview)
        ans = QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return ans == QMessageBox.Yes

    def _preview_upload_files(self, pkg_dir: Path) -> list[str]:
        ok, msg = self._run(["git", "status", "--porcelain"], cwd=pkg_dir)
        if not ok:
            return [f"[无法预览] {msg}"]
        lines = [line for line in (msg or "").splitlines() if line.strip()]
        return lines

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
        return lines

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
                commit_msg = f"sync {pkg_name}\n\nl_repo_sync_gui automated commit"
                ok_commit, msg_commit = self._run(
                    ["git", "commit", "-m", commit_msg], cwd=pkg_dir
                )
                if not ok_commit:
                    self._log(f"[WARN] {pkg_name} commit 失败: {msg_commit}")
            else:
                self._log(f"[info] {pkg_name} no changes to commit")

            ok_push, msg_push = self._run(
                ["git", "push", "-u", "origin", branch], cwd=pkg_dir
            )
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


def main():
    app = QApplication(sys.argv)
    win = RepoSyncWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
