"""开发入口：直接在源码目录里跑 CLI，不需要可编辑安装。

为什么需要它（继承自 LabBox 的真实 Windows 发现，见 DEVELOPMENT.md §22）：
可编辑安装会写一个含绝对源码路径的 ``.pth``，而 ``site.py`` 用 *locale* 编码去解码 ``.pth``。
在 cp936 机器上，路径含非 ASCII（``C:\\code\\github小项目``）时解出来是乱码，
``import timetrace`` 直接失败，``timetrace`` 命令行脚本也就起不来。
所以本机开发统一走这个 shim。

普通 wheel 安装没有这个问题（2026-10-12 实测）：wheel 不写那种 .pth，
装到含中文的 venv 里、--home 再指向中文目录，十条命令全部 exit 0。
踩坑的只有 pip install -e（可编辑安装）—— 它的 .pth 里存的就是源码绝对路径。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from timetrace.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
