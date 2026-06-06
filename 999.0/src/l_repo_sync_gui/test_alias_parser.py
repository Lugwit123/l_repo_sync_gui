# -*- coding: utf-8 -*-
"""测试 Rez package.py 里的 alias(name, command) 解析结果。

用法：
    python test_alias_parser.py D:/path/to/package.py
    python test_alias_parser.py D:/path/to/package_dir

不传参数时，会测试当前 l_repo_sync_gui 包的 package.py。
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path


def parse_rez_package_aliases(package_text: str) -> list[tuple[str, str]]:
    """从 Rez package.py 文本中解析 alias(name, command)，不执行 package.py。"""
    aliases: list[tuple[str, str]] = []
    try:
        tree = ast.parse(package_text)
    except SyntaxError:
        return aliases
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "alias" or len(node.args) < 2:
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
    """生成通过 wuwo 启动 alias 名的文本。"""
    if wuwo_bat is not None:
        return f"wuwor {pkg_name} .ps .solo -- {alias_name}"
    return f"wuwo {pkg_name} .ps .solo -- {alias_name}"


def _iter_package_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(path.rglob("package.py"), key=lambda p: str(p).lower())
    return []


def main() -> int:
    """解析参数并打印 alias 测试结果。"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        nargs="?",
        default=str(Path(__file__).resolve().parents[2] / "package.py"),
        help="package.py 文件或包目录；默认测试当前 l_repo_sync_gui 包",
    )
    parser.add_argument(
        "--pkg-name",
        default="",
        help="用于生成 rez env 命令的包名；默认使用 package.py 的上两级目录名",
    )
    parser.add_argument(
        "--wuwo-bat",
        default="",
        help="可选 wuwo.bat 路径，用于生成完整启动命令",
    )
    args = parser.parse_args()

    target = Path(args.path).resolve()
    files = _iter_package_files(target)
    if not files:
        print(f"未找到 package.py: {target}")
        return 1

    wuwo_bat = Path(args.wuwo_bat).resolve() if args.wuwo_bat else None
    total = 0
    for package_py in files:
        text = package_py.read_text(encoding="utf-8", errors="ignore")
        aliases = parse_rez_package_aliases(text)
        pkg_name = args.pkg_name or package_py.parents[1].name
        print(f"\npackage.py: {package_py}")
        print(f"pkg_name: {pkg_name}")
        if not aliases:
            print("  未解析到 alias(name, command)")
            continue
        for alias_name, alias_command in aliases:
            total += 1
            launch_cmd = format_rez_alias_launch_cmd(
                pkg_name,
                alias_name,
                wuwo_bat=wuwo_bat,
            )
            print(f"  alias name   : {alias_name}")
            print(f"  alias command: {alias_command}")
            print(f"  launch cmd   : {launch_cmd}  # {{{alias_command}}}")
    print(f"\n总 alias 数: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
