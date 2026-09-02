# -*- coding: utf-8 -*-
"""
程序入口：python main.py
"""
import os
import sys

# 保证无论从哪里启动都能 import 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app  # noqa: E402


def main():
    try:
        app.run()
    except Exception as exc:
        try:
            from tkinter import messagebox
            messagebox.showerror("程序错误", "发生错误：\n%s" % exc)
        except Exception:
            raise


if __name__ == "__main__":
    main()
