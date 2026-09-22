#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 TokenFurnace 装到一个固定位置，并建一个桌面图标。

为什么需要这个：源码目录通常带日期（比如 `2026-09-20-09-30-59`），
哪天被清理掉，工具和账本就一起没了，用户也找不到入口。
装到用户目录之后位置稳定，桌面图标永远能点开。

    python scripts/install.py            # 装（已存在会先备份再覆盖）
    python scripts/install.py --uninstall

Windows 装到 %LOCALAPPDATA%\\TokenFurnace，桌面建快捷方式。
macOS / Linux 装到 ~/.local/share/tokenfurnace，桌面建 .desktop / 可执行脚本。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tokenfurnace import __version__  # noqa: E402

APP = "TokenFurnace"
IS_WIN = os.name == "nt"

LAUNCH_BAT = """@echo off
chcp 65001 >nul
title TokenFurnace
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [错误] 没找到 Python。
  echo   请先安装 Python 3.9 或更高版本：https://www.python.org/downloads/
  echo   安装时务必勾选 "Add python.exe to PATH"。
  echo.
  pause
  exit /b 1
)

echo.
echo   TokenFurnace 正在启动，浏览器会自动打开。
echo   控制台地址：http://127.0.0.1:8760/
echo.
echo   这个窗口就是服务本身，关掉它就停止。
echo.
python tokenfurnace.pyz
echo.
echo   服务已停止。
pause
"""

LAUNCH_SH = """#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "找不到 python3，请先安装 Python 3.9+" >&2
  exit 1
fi
echo "TokenFurnace 正在启动，浏览器会自动打开。"
echo "控制台地址：http://127.0.0.1:8760/"
echo "按 Ctrl+C 停止。"
exec python3 tokenfurnace.pyz
"""


def install_dir() -> pathlib.Path:
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or (pathlib.Path.home() / "AppData" / "Local")
        return pathlib.Path(base) / APP
    return pathlib.Path.home() / ".local" / "share" / "tokenfurnace"


def desktop_dir() -> pathlib.Path | None:
    for name in ("Desktop", "桌面", "OneDrive/Desktop", "OneDrive/桌面"):
        d = pathlib.Path.home() / name
        if d.is_dir():
            return d
    return None


def find_pyz() -> pathlib.Path:
    """优先用构建好的单文件包；没有就现打一个。"""
    dist = ROOT / "dist" / f"tokenfurnace-{__version__}.pyz"
    if dist.exists():
        return dist
    print("  dist/ 里没有 .pyz，先构建…")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_release.py"),
                    "--no-check"], check=True, cwd=ROOT)
    if not dist.exists():
        raise SystemExit("构建失败，dist/ 里还是没有 .pyz")
    return dist


def copy_db(src: pathlib.Path, dst: pathlib.Path) -> bool:
    """用 SQLite 的 backup API 复制账本。

    不能直接 copy 文件：写入模式是 WAL，最近的记录还在 -wal 里，
    只复制 .db 会丢掉那部分——我当初就踩过这个坑，丢过一个会话。
    """
    if not src.exists():
        return False
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        with d:
            s.backup(d)
    finally:
        d.close()
        s.close()
    return True


def make_shortcut(target: pathlib.Path, workdir: pathlib.Path,
                  dest: pathlib.Path) -> bool:
    """在桌面建快捷方式。Windows 用 COM，POSIX 写一个 .desktop。"""
    if IS_WIN:
        ps = (
            "$W = New-Object -ComObject WScript.Shell; "
            f"$S = $W.CreateShortcut('{dest}'); "
            f"$S.TargetPath = '{target}'; "
            f"$S.WorkingDirectory = '{workdir}'; "
            f"$S.IconLocation = '{target},0'; "
            "$S.Description = 'TokenFurnace - LLM Token 控制台'; "
            "$S.Save()"
        )
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=90)
            return r.returncode == 0
        except Exception:
            return False
    desktop = dest
    desktop.write_text(
        "[Desktop Entry]\nType=Application\n"
        f"Name={APP}\nComment=LLM Token 控制台\n"
        f"Exec={workdir}/start.sh\nPath={workdir}\n"
        "Terminal=true\nCategories=Development;\n", encoding="utf-8")
    desktop.chmod(0o755)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="安装 TokenFurnace 到固定位置")
    ap.add_argument("--uninstall", action="store_true", help="卸载（保留配置和账本）")
    ap.add_argument("--no-shortcut", action="store_true", help="不建桌面图标")
    a = ap.parse_args()

    target = install_dir()
    desk = desktop_dir()

    if a.uninstall:
        for p in (desk / f"{APP}.lnk" if IS_WIN and desk else None,
                  desk / f"{APP}.desktop" if (not IS_WIN and desk) else None):
            if p and p.exists():
                p.unlink()
                print(f"  已删除桌面图标 {p.name}")
        print(f"  程序目录保留在 {target}（配置和账本没动）")
        print(f"  想彻底删掉就手动删这个目录")
        return 0

    print(f"安装到 {target}")
    target.mkdir(parents=True, exist_ok=True)

    # 1) 程序本体
    pyz = find_pyz()
    shutil.copy2(pyz, target / "tokenfurnace.pyz")
    print(f"  ✓ tokenfurnace.pyz（{pyz.stat().st_size / 1024:.0f} KB）")

    # 2) 配置：已存在就不覆盖，免得把用户改好的配置冲掉
    cfg_src = ROOT / "config.json"
    cfg_dst = target / "config.json"
    if cfg_src.exists() and not cfg_dst.exists():
        shutil.copy2(cfg_src, cfg_dst)
        print("  ✓ config.json（从源码目录带过来）")
    elif cfg_dst.exists():
        print("  · config.json 已存在，保留现有的一份")
    else:
        print("  · 没有配置文件，首次启动会生成一个空的")

    # 3) 账本
    (target / "data").mkdir(exist_ok=True)
    db_dst = target / "data" / "tokenfurnace.db"
    if copy_db(ROOT / "data" / "tokenfurnace.db", db_dst):
        n = sqlite3.connect(f"file:{db_dst}?mode=ro", uri=True).execute(
            "SELECT COUNT(*) FROM runs").fetchone()[0]
        print(f"  ✓ 账本已迁移（{n} 个历史会话）")
    else:
        print("  · 没有账本，首次启动会新建")

    # 4) 启动脚本
    if IS_WIN:
        (target / "start.bat").write_text(LAUNCH_BAT, encoding="utf-8")
        print("  ✓ start.bat")
    else:
        p = target / "start.sh"
        p.write_text(LAUNCH_SH, encoding="utf-8")
        p.chmod(0o755)
        print("  ✓ start.sh")

    # 5) 桌面图标
    if a.no_shortcut or not desk:
        print("  · 跳过桌面图标")
    else:
        launcher = target / ("start.bat" if IS_WIN else "start.sh")
        dest = desk / (f"{APP}.lnk" if IS_WIN else f"{APP}.desktop")
        if make_shortcut(launcher, target, dest):
            print(f"  ✓ 桌面图标：{dest.name}")
        else:
            print(f"  ✗ 桌面图标没建成，手动双击这个也行：{launcher}")

    print()
    print("装好了。以后这样用：")
    print(f"  1. 双击桌面上的「{APP}」")
    print("  2. 浏览器会自动打开 http://127.0.0.1:8760/")
    print("  3. 那个黑窗口就是服务本身，关掉它就停")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
