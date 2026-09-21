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
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipapp

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tokenfurnace import __version__  # noqa: E402

PACKAGE = "tokenfurnace"
ENTRY = f"{PACKAGE}.cli:main"
# 只把这些打进包里，测试、脚本、文档都不需要
EXCLUDE_SUFFIX = {".pyc", ".pyo"}


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
    code = (
        "import json,sys,threading,urllib.request\n"
        "from http.server import ThreadingHTTPServer\n"
        "import tempfile,pathlib\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from tokenfurnace.server import App, Handler\n"
        "from tokenfurnace.providers import PROTOCOLS\n"
        "tmp = tempfile.mkdtemp()\n"
        "app = App(pathlib.Path(tmp)); Handler.app = app\n"
        "httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)\n"
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
    ap = argparse.ArgumentParser(description="构建单文件 .pyz 发布包")
    ap.add_argument("-o", "--out", default=None, help="输出路径")
    ap.add_argument("--no-check", action="store_true", help="跳过自检")
    a = ap.parse_args()

    out = pathlib.Path(a.out) if a.out else ROOT / "dist" / f"tokenfurnace-{__version__}.pyz"
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="tf-build-") as tmp:
        stage(pathlib.Path(tmp))
        zipapp.create_archive(tmp, target=out, interpreter="/usr/bin/env python3",
                              main=ENTRY, compressed=True)

    size_kb = out.stat().st_size / 1024
    print(f"已生成 {out.relative_to(ROOT)}  ({size_kb:.0f} KB)")

    if not a.no_check:
        print("自检中…")
        self_check(out)

    print(f"\n跑法： python {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
