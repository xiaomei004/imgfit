"""PyInstaller 打包入口：把图形界面打成单文件 exe。

只是为了 ``python -m PyInstaller ... tools/exe_entry.py`` 有个明确的入口，
不参与常规运行（常规用法是 ``imgfit-gui`` / ``python -m imgfit --gui``）。
"""

import sys

from imgfit.gui import main

if __name__ == "__main__":
    sys.exit(main())
