#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把整个工具打成一个 .pyz 单文件，作为 Release 附件。

零依赖工具最省事的发布形态：用户下载一个文件，`python tokenfurnace.pyz` 就能跑，
不用 clone、不用 pip、不用管目录结构。

    python scripts/build_release.py

产出 dist/tokenfurnace-<版本>.pyz，并顺手做一次自检（真起服务、打 API）。
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tokenfurnace import __version__  # noqa: E402

PACKAGE = "tokenfurnace"
ENTRY = f"{PACKAGE}.cli:main"
# 只把这些打进包里，测试、脚本、文档都不需要
EXCLUDE_SUFFIX = {".pyc", ".pyo"}

# 双击启动脚本。文件名用 ASCII，因为中文文件名经过 zip 传递到 Windows
# 有可能变成乱码；中文放在文件内容里，配合 chcp 65001 显示正常。
LAUNCHER_BAT = """@echo off
chcp 65001 >nul
title TokenFurnace
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [错误] 没找到 Python。
  echo.
  echo   请先安装 Python 3.9 或更高版本：
  echo     https://www.python.org/downloads/
  echo   安装时务必勾选 "Add python.exe to PATH"。
  echo.
  pause
  exit /b 1
)

echo   正在启动 TokenFurnace，浏览器会自动打开…
echo   关掉这个窗口就停止服务。
echo.
python tokenfurnace.pyz
pause
"""

LAUNCHER_SH = """#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "找不到 python3，请先安装 Python 3.9+" >&2
  exit 1
fi
echo "正在启动 TokenFurnace，浏览器会自动打开…按 Ctrl+C 停止。"
exec python3 tokenfurnace.pyz
"""

README_TXT = """TokenFurnace v{version}
========================================

双击 start.bat（Windows）或运行 start.sh（macOS / Linux）即可。

前提：装了 Python 3.9 或更高版本。
没装的话去 https://www.python.org/downloads/ 下载，
安装时记得勾选 "Add python.exe to PATH"。

不想双击的话，在终端里跑：
    python tokenfurnace.pyz

配置和账本会生成在这个目录下：
    config.json            你的配置（含密钥，别提交到 git）
    data/tokenfurnace.db   记账库

默认只监听 127.0.0.1:8760，不对外网开放。
"""


# zip 能表示的最早时间。固定它，构建才可复现。
FIXED_MTIME = (1980, 1, 1, 0, 0, 0)


def build_pyz(src_dir: pathlib.Path, target: pathlib.Path,
              interpreter: str = "/usr/bin/env python3") -> None:
    """自己打包而不是用 zipapp.create_archive。

    原因只有一个：**构建要可复现**。zipapp 会把文件的实际 mtime 写进 zip，
    同样的源码在不同时刻构建出来字节不同、SHA256 不同——
    那 release notes 里公布的校验和就没法让用户独立核对。
    这里把时间戳固定成 zip 的下限，并固定权限位，同一份源码永远产出同样的字节。
    """
    module, func = ENTRY.split(":")
    # 必须 import 完整的模块路径：只 import 顶层包不会把子模块带进来，
    # 后面调用 module.func() 就会 AttributeError
    entry = (f"# -*- coding: utf-8 -*-\n"
             f"import {module}\n"
             f"{module}.{func}()\n")
    with open(target, "wb") as f:
        f.write(f"#!{interpreter}\n".encode("utf-8"))
        with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            def put(name: str, data: bytes, mode: int = 0o644) -> None:
                zi = zipfile.ZipInfo(name, FIXED_MTIME)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = mode << 16
                z.writestr(zi, data)

            put("__main__.py", entry.encode("utf-8"), 0o755)
            for p in sorted(src_dir.rglob("*")):
                if not p.is_file() or p.suffix in EXCLUDE_SUFFIX:
                    continue
                if "__pycache__" in p.parts:
                    continue
                put(p.relative_to(src_dir).as_posix(), p.read_bytes())


def build_windows_zip(pyz: pathlib.Path, out_dir: pathlib.Path) -> pathlib.Path:
    """把 .pyz 和双击启动脚本打成一个压缩包。

    单文件 .pyz 虽然最省事，但双击没反应（Windows 默认没给 .pyz 关联打开方式），
    对不熟悉终端的用户是个坎。这个包解压后双击就能起。

    包内的 .pyz 不带版本号，这样 start.bat 不用跟着版本改。
    时间戳同样固定，保证可复现。
    """
    out = out_dir / f"tokenfurnace-{__version__}-windows.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        def put(name: str, data: bytes, mode: int = 0o644) -> None:
            zi = zipfile.ZipInfo(name, FIXED_MTIME)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = mode << 16
            z.writestr(zi, data)

        put("tokenfurnace.pyz", pyz.read_bytes(), 0o755)
        put("start.bat", LAUNCHER_BAT.encode("utf-8"))
        # 可执行位必须显式写进 zip。从 Windows 打包时文件系统没有这个位，
        # 不写的话解压到 macOS / Linux 后 start.sh 双击没反应，
        # 用户得自己 chmod +x——这种坑不该让用户踩。
        put("start.sh", LAUNCHER_SH.encode("utf-8"), 0o755)
        put("README.txt", README_TXT.format(version=__version__).encode("utf-8"))
    return out


def sha256_of(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def stage(dest: pathlib.Path) -> None:
    """把包拷到暂存目录，剔除 __pycache__ 之类。"""
    src = ROOT / PACKAGE
    dst = dest / PACKAGE
    shutil.copytree(src, dst,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))


def self_check(pyz: pathlib.Path) -> None:
    """真跑一次：起服务、打 /api/state 和首页、断言、关掉。

    单文件包最容易坏在「资源读不出来」——静态文件如果还拼文件系统路径，
    压缩包内就找不到。这里必须真打一次才算数。
    """
    # 先把它当**程序**跑一遍。这一步不能省：只 import 包内模块的话，
    # __main__.py 写错（比如 import 了顶层包却没 import 子模块）也照样通过，
    # 用户拿到手才发现「python xxx.pyz」直接报错。
    r = subprocess.run([sys.executable, str(pyz), "--version"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print(r.stderr[-1500:], file=sys.stderr)
        raise SystemExit("❌ 单文件包无法作为程序运行")
    print(f"  作为程序运行正常：{r.stdout.strip()}")

    code = (
        "import json,sys,threading,urllib.request\n"
        "from http.server import ThreadingHTTPServer\n"
        "import tempfile,pathlib\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from tokenfurnace.server import App, Handler, Server\n"
        "from tokenfurnace.providers import PROTOCOLS\n"
        "tmp = tempfile.mkdtemp()\n"
        "app = App(pathlib.Path(tmp)); Handler.app = app\n"
        "httpd = Server(('127.0.0.1', 0), Handler)\n"
        "port = httpd.server_address[1]\n"
        "threading.Thread(target=httpd.serve_forever, daemon=True).start()\n"
        "b = f'http://127.0.0.1:{port}'\n"
        "st = json.loads(urllib.request.urlopen(b + '/api/state', timeout=15).read())\n"
        "assert set(st['protocols']) == set(PROTOCOLS), 'protocols 不对'\n"
        "html = urllib.request.urlopen(b + '/', timeout=15).read().decode()\n"
        "assert 'app.js' in html, '首页没读到'\n"
        "js = urllib.request.urlopen(b + '/static/app.js', timeout=15).read()\n"
        "assert len(js) > 10000, 'app.js 没读到'\n"
        "css = urllib.request.urlopen(b + '/static/style.css', timeout=15).read()\n"
        "assert len(css) > 5000, 'style.css 没读到'\n"
        "httpd.shutdown(); httpd.server_close(); app.store.close()\n"
        "print(f'  包内资源可读：首页 {len(html)} 字节 / app.js {len(js)} 字节 '\n"
        "      f'/ style.css {len(css)} 字节')\n"
    )
    r = subprocess.run([sys.executable, "-c", code, str(pyz)],
                       capture_output=True, text=True, timeout=180)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        print(r.stderr[-2000:], file=sys.stderr)
        raise SystemExit("❌ 自检失败：单文件包跑不起来")


def main() -> int:
    ap = argparse.ArgumentParser(description="构建发布附件：单文件 .pyz + 双击启动包")
    ap.add_argument("-o", "--out", default=None, help=".pyz 输出路径")
    ap.add_argument("--no-check", action="store_true", help="跳过自检")
    a = ap.parse_args()

    out = pathlib.Path(a.out) if a.out else ROOT / "dist" / f"tokenfurnace-{__version__}.pyz"
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="tf-build-") as tmp:
        stage(pathlib.Path(tmp))
        build_pyz(pathlib.Path(tmp), out)

    z = build_windows_zip(out, out.parent)

    print("已生成发布附件：")
    for f in (out, z):
        print(f"  {f.name:<38} {f.stat().st_size / 1024:>6.1f} KB")
        print(f"    SHA256 {sha256_of(f)}")
    print(f"\n  {z.name} 内含：tokenfurnace.pyz / start.bat / start.sh / README.txt")

    if not a.no_check:
        print("\n自检中…")
        self_check(out)

    print(f"\n两种跑法：")
    print(f"  终端： python {out.name}")
    print(f"  双击： 解压 {z.name}，运行里面的 start.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
