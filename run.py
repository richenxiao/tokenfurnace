#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TokenFurnace 启动器（便捷入口）。

等价于 `python -m tokenfurnace`。安装后也可以用 `tokenfurnace` 命令。

    python run.py
    python run.py --port 9000
    python run.py --no-browser
    python run.py --config my.json
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tokenfurnace.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
