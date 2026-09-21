"""命令行入口。

    python -m tokenfurnace            # 等价于 run.py
    tokenfurnace                      # 用 pyproject 安装后可直接调用
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="tokenfurnace",
        description="TokenFurnace · LLM Token 消费与额度管理（本地 Web 控制台）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  tokenfurnace                          默认 127.0.0.1:8760，自动打开浏览器
  tokenfurnace --port 9000              换端口
  tokenfurnace --no-browser             服务器 / 远程场景
  tokenfurnace --config my.json         指定配置文件
  tokenfurnace --host 0.0.0.0           允许局域网访问（自行注意安全）
""")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址，默认 127.0.0.1（仅本机可访问）")
    ap.add_argument("--port", type=int, default=8760, help="监听端口，默认 8760")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--config", default=None, help="配置文件路径，默认 ./config.json")
    ap.add_argument("--version", action="version", version=f"TokenFurnace {__version__}")
    return ap


def main(argv=None) -> int:
    if sys.version_info < (3, 9):
        print(f"需要 Python 3.9 及以上，当前 {sys.version.split()[0]}", file=sys.stderr)
        return 1
    args = build_parser().parse_args(argv)

    from .server import serve
    # 配置和账本都落在当前工作目录，源码克隆和 pip 安装两种装法行为一致
    serve(Path.cwd(),
          host=args.host,
          port=args.port,
          open_browser=not args.no_browser,
          config_path=Path(args.config).expanduser() if args.config else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
