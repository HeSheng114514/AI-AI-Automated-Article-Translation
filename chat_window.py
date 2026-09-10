# -*- coding: utf-8 -*-
"""
对话翻译窗口（左右双面板，类 Google 翻译样式）
================================================
左边写文本，右边显示 AI 译文；顶部两个下拉框选源语言/目标语言，中间 ↔ 一键互换。

翻译方式：**点“▶ 翻译”按钮（或 Ctrl+Enter）才翻译**，输入过程中不会自动请求。
朗读使用 Windows 自带语音；无论何时关闭窗口或退出程序，都会立即终止后台朗读进程。
纯本地使用，不读写任何文件。
"""
import atexit
import os
import queue
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import langs
from ai_client import AIClient

# 常用短语（按源语言给出，点击填入输入框）
CHIPS = {
    "en": ["Hello", "How are you?", "Thank you", "Goodbye"],
    "zh": ["你好", "你好吗？", "谢谢", "再见"],
    "ja": ["こんにちは", "お元気ですか？", "ありがとう", "さようなら"],
    "ko": ["안녕하세요", "잘 지내세요?", "감사합니다", "안녕히 가세요"],
    "fr": ["Bonjour", "Comment allez-vous ?", "Merci", "Au revoir"],
    "de": ["Hallo", "Wie geht es Ihnen?", "Danke", "Auf Wiedersehen"],
    "ru": ["Привет", "Как дела?", "Спасибо", "До свидания"],
    "es": ["Hola", "¿Cómo estás?", "Gracias", "Adiós"],
}

_TONES = {"默认": "", "非正式": " Use a casual, colloquial tone.", "正式": " Use a formal, polite tone."}

# 正在朗读的后台进程（退出程序时统一终止，保证不会“关掉软件还在读”）
_ACTIVE_TTS = set()


def _kill_proc(proc):
    try:
        if proc is not None and proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    _ACTIVE_TTS.discard(proc)


def stop_all_speech():
    """终止所有正在朗读的后台进程（供窗口/程序关闭时调用）。"""
    for proc in list(_ACTIVE_TTS):
        _kill_proc(proc)


atexit.register(stop_all_speech)


class ChatTranslateWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.q = queue.Queue()
        self._busy = False
        self._closing = False
        self._chips = []
        self._chip_lang = None
        self._chip_layout = None   # (宽度, 个数) 避免重复排版
        self._tts_proc = None      # 当前朗读进程
        self._tts_text = None      # 当前朗读内容（用于再次点击=停止）

        self.title("对话翻译")
        self.geometry("940x600")
        self.minsize(760, 500)
        self.transient(app.root)

        self._build_ui()

        # 初始语言方向：沿用主界面/上次保存的设置
        src, dst = langs.direction_pair(self.app.settings.direction)
        self.var_src.set(langs.display_name(src))
        self.var_dst.set(langs.display_name(dst))
        self._rebuild_chips()
        self._refresh_profile()

        self.after(100, self._poll_queue)
        self.txt_in.focus_set()

    # ==================================================================
    # 界面
    # ==================================================================
    def _build_ui(self):
        # ---------- 顶部：语言选择 + 交换 ----------
        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        top.columnconfigure(0, weight=1)
        top.columnconfigure(2, weight=1)

        self.var_src = tk.StringVar()
        self.cb_src = ttk.Combobox(top, textvariable=self.var_src, state="readonly",
                                   values=langs.source_language_options(), font=("Microsoft YaHei UI", 10))
        self.cb_src.grid(row=0, column=0, sticky="ew")
        self.cb_src.bind("<<ComboboxSelected>>", lambda e: self._on_lang_change())

        tk.Button(top, text="↔", width=3, font=("Segoe UI Symbol", 14),
                  relief="flat", bd=0, bg="#f1f3f4", activebackground="#e8eaed",
                  cursor="hand2", command=self._swap).grid(row=0, column=1, padx=8)

        self.var_dst = tk.StringVar()
        self.cb_dst = ttk.Combobox(top, textvariable=self.var_dst, state="readonly",
                                   values=langs.target_language_options(), font=("Microsoft YaHei UI", 10))
        self.cb_dst.grid(row=0, column=2, sticky="ew")
        self.cb_dst.bind("<<ComboboxSelected>>", lambda e: self._on_lang_change())

        # ---------- 主体：左右两个面板 ----------
        body = ttk.Frame(self, padding=(10, 0, 10, 0))
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1, uniform="panel")
        body.columnconfigure(2, weight=1, uniform="panel")

        # 左：输入面板
        left = tk.Frame(body, bg="#ffffff", highlightthickness=1, highlightbackground="#dcdcdc")
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        self.txt_in = tk.Text(left, wrap="word", font=("Microsoft YaHei UI", 20),
                              height=9, relief="flat", bg="#ffffff", fg="#202124",
                              insertbackground="#202124", padx=14, pady=12, undo=True)
        self.txt_in.grid(row=0, column=0, sticky="nsew")
        self.txt_in.bind("<Control-Return>", self._on_hotkey)

        chips_row = tk.Frame(left, bg="#ffffff")
        chips_row.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))
        self.chips_row = chips_row
        self.chips_row.bind("<Configure>", self._layout_chips)

        bar_l = tk.Frame(left, bg="#ffffff")
        bar_l.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))
        self.btn_go = ttk.Button(bar_l, text="▶ 翻译", width=10, command=self._translate)
        self.btn_go.pack(side="left")
        ttk.Button(bar_l, text="✕ 清空", width=8, command=self._clear_all).pack(side="left", padx=(6, 0))
        ttk.Button(bar_l, text="🔊 朗读", width=8,
                   command=lambda: self._speak(self.txt_in.get("1.0", "end").strip())).pack(side="right")
        ttk.Button(bar_l, text="⧉ 复制", width=8,
                   command=lambda: self._copy(self.txt_in.get("1.0", "end").strip(), "原文")).pack(side="right", padx=(0, 6))

        # 中间分隔线
        sep = tk.Frame(body, bg="#dcdcdc", width=1)
        sep.grid(row=0, column=1, sticky="ns", padx=8)

        # 右：译文面板
        right = tk.Frame(body, bg="#ffffff", highlightthickness=1, highlightbackground="#dcdcdc")
        right.grid(row=0, column=2, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.txt_out = tk.Text(right, wrap="word", font=("Microsoft YaHei UI", 20, "bold"),
                               height=9, relief="flat", bg="#ffffff", fg="#1a73e8",
                               padx=14, pady=12, state="disabled")
        self.txt_out.grid(row=0, column=0, sticky="nsew")

        bar_r = tk.Frame(right, bg="#ffffff")
        bar_r.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))
        ttk.Label(bar_r, text="风格").pack(side="left")
        self.var_tone = tk.StringVar(value="默认")
        cb_tone = ttk.Combobox(bar_r, textvariable=self.var_tone, state="readonly",
                               values=list(_TONES.keys()), width=6)
        cb_tone.pack(side="left", padx=(4, 0))
        cb_tone.bind("<<ComboboxSelected>>", lambda e: self._on_tone_change())
        ttk.Button(bar_r, text="🔊 朗读", width=8,
                   command=lambda: self._speak(self.txt_out.get("1.0", "end").strip())).pack(side="right")
        ttk.Button(bar_r, text="⧉ 复制", width=8,
                   command=lambda: self._copy(self.txt_out.get("1.0", "end").strip(), "译文")).pack(side="right", padx=(0, 6))

        # ---------- 底部状态栏 ----------
        bottom = ttk.Frame(self, padding=(10, 4, 10, 8))
        bottom.pack(fill="x")
        self.lbl_profile = ttk.Label(bottom, text="", foreground="#666666")
        self.lbl_profile.pack(side="left")
        ttk.Button(bottom, text="设置…", width=7, command=self._open_settings).pack(side="left", padx=6)
        self.lbl_status = ttk.Label(bottom, text="", foreground="#666666")
        self.lbl_status.pack(side="right")
        self._set_status("写好后点“▶ 翻译”或按 Ctrl+Enter（不会随输入自动翻译）")

    # ==================================================================
    # 语言 / 短语
    # ==================================================================
    def _pair(self):
        return langs.display_code(self.var_src.get()), langs.display_code(self.var_dst.get())

    def _swap(self):
        s, d = self.var_src.get(), self.var_dst.get()
        self.var_src.set(d if d in langs.source_language_options() else langs.display_name("en"))
        self.var_dst.set(s)
        self._rebuild_chips()
        self._mark_stale("已互换语言，点“▶ 翻译”重新翻译")

    def _on_lang_change(self):
        self._rebuild_chips()
        self._mark_stale("已切换语言，点“▶ 翻译”重新翻译")

    def _on_tone_change(self):
        self._mark_stale("已切换风格，点“▶ 翻译”重新翻译")

    def _mark_stale(self, hint):
        """语言/风格变化后清掉旧译文，避免显示与当前设置不符的结果。"""
        if self.txt_out.get("1.0", "end").strip():
            self._set_output("")
        self._set_status(hint, "#b06000")

    def _rebuild_chips(self):
        src, _dst = self._pair()
        if src == "auto":
            det = langs.detect_language(self.txt_in.get("1.0", "end"))
            code = det if det in CHIPS else "en"
        else:
            code = src
        if code == self._chip_lang and self._chips:
            return
        self._chip_lang = code
        for w in self._chips:
            w.destroy()
        self._chips = []
        self._chip_layout = None
        phrases = CHIPS.get(code, CHIPS["en"])
        for ph in phrases:
            b = tk.Button(self.chips_row, text=ph, relief="solid", bd=1, bg="#ffffff",
                          activebackground="#f1f3f4", fg="#3c4043",
                          font=("Microsoft YaHei UI", 9), padx=10, pady=1,
                          command=lambda p=ph: self._use_chip(p))
            self._chips.append(b)
        self.chips_row.update_idletasks()
        self._layout_chips()

    def _layout_chips(self, event=None):
        """示例短语按面板实际宽度自动换行摆放，避免被截断。"""
        if not self._chips:
            return
        panel_w = self.txt_in.master.winfo_width()
        width = (panel_w if panel_w > 1 else (event.width if event is not None else 0)) - 28
        if width <= 60:
            return
        if self._chip_layout == (width, len(self._chips)):
            return
        self._chip_layout = (width, len(self._chips))
        x = 0
        row = 0
        col = 0
        for b in self._chips:
            b.update_idletasks()
            bw = b.winfo_reqwidth() + 8
            if col and x + bw > width:
                row += 1
                col = 0
                x = 0
            b.grid(row=row, column=col, padx=(0, 6), pady=2, sticky="w")
            x += bw
            col += 1

    def _use_chip(self, phrase):
        """示例短语只填入输入框，不自动翻译。"""
        self.txt_in.delete("1.0", "end")
        self.txt_in.insert("1.0", phrase)
        self.txt_in.focus_set()
        self._set_status("已填入示例，点“▶ 翻译”开始翻译")

    # ==================================================================
    # 翻译（仅点击触发）
    # ==================================================================
    def _on_hotkey(self, _event=None):
        self._translate()
        return "break"

    def _configured(self):
        s = self.app.settings
        return bool(s.base_url and s.api_key and s.model)

    def _translate(self):
        text = self.txt_in.get("1.0", "end").strip()
        if not text:
            self._set_status("请先输入要翻译的内容", "#b06000")
            self.txt_in.focus_set()
            return
        if not self._configured():
            self._set_status("尚未配置接口，请点“设置…”填写 API Key", "#b06000")
            return
        src, dst = self._pair()
        if src != "auto" and src == dst:
            self._set_status("源语言与目标语言相同", "#b06000")
            return
        if self._busy:
            self._set_status("正在翻译，请稍候…", "#aa6600")
            return

        self._busy = True
        self.btn_go.configure(state="disabled")
        self._set_status("翻译中…", "#aa6600")
        pair = (src, dst, self.var_tone.get())
        snapshot = (self.app.settings.base_url, self.app.settings.api_key,
                    self.app.settings.model, self.app.settings.ai_timeout)
        threading.Thread(target=self._worker, args=(text, pair, snapshot), daemon=True).start()

    def _worker(self, text, pair, snapshot):
        base_url, api_key, model, timeout = snapshot
        src, dst, tone = pair
        client = AIClient(base_url, api_key, model, timeout=timeout)
        try:
            messages = self._build_messages(text, src, dst, tone)
            result = client.chat(messages, temperature=0.2)
        except Exception as exc:
            self.q.put(("error", str(exc)))
            return
        self.q.put(("done", result))

    @staticmethod
    def _build_messages(text, src, dst, tone):
        target = langs.target_language_name(dst)
        source = "the source language" if src == "auto" else langs.target_language_name(src)
        style = _TONES.get(tone, "")
        system = ("You are a professional translator. Translate the text from %s into %s.%s "
                  "Preserve line breaks, numbers, code, URLs and formatting. "
                  "Reply with the translation only, no explanations." % (source, target, style))
        return [{"role": "system", "content": system},
                {"role": "user", "content": text}]

    # ==================================================================
    # 线程 -> 界面（主线程轮询，安全）
    # ==================================================================
    def _poll_queue(self):
        if self._closing:
            return
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "done":
                    self._on_done(item[1])
                elif item[0] == "error":
                    self._on_error(item[1])
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.after(120, self._poll_queue)
        except tk.TclError:
            pass

    def _on_done(self, result):
        self._busy = False
        try:
            self.btn_go.configure(state="normal")
        except tk.TclError:
            return
        self._set_output(result)
        self._set_status("完成 · %s → %s" % (self.var_src.get(), self.var_dst.get()), "#0a7d0a")

    def _on_error(self, err):
        self._busy = False
        try:
            self.btn_go.configure(state="normal")
        except tk.TclError:
            return
        self._set_status("翻译失败：%s" % err[:80], "#cc0000")

    def _set_output(self, text):
        self.txt_out.configure(state="normal")
        self.txt_out.delete("1.0", "end")
        if text:
            self.txt_out.insert("1.0", text)
        self.txt_out.configure(state="disabled")

    def _set_status(self, text, color="#666666"):
        try:
            self.lbl_status.configure(text=text, foreground=color)
        except tk.TclError:
            pass

    # ==================================================================
    # 复制 / 朗读 / 清空 / 设置
    # ==================================================================
    def _copy(self, text, which):
        if not text:
            self._set_status("没有可复制的%s" % which, "#b06000")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._set_status("已复制%s到剪贴板" % which, "#0a7d0a")

    def _speak(self, text):
        """
        朗读：再次点击同一段内容=停止朗读；窗口/程序关闭时也会立即停止。
        使用 Windows 自带语音（不可用时仅提示，不影响翻译）。
        """
        # 正在朗读同一段 -> 停止
        if self._tts_proc is not None and self._tts_proc.poll() is None and text == self._tts_text:
            self.stop_speak()
            self._set_status("已停止朗读", "#666666")
            return
        # 换了内容或另一侧 -> 先停掉旧的，再开始新的
        self.stop_speak()
        if not text:
            self._set_status("没有可朗读的内容", "#b06000")
            return
        try:
            path = os.path.join(tempfile.gettempdir(), "dsh_tts_%d.txt" % os.getpid())
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            esc = path.replace("'", "''")
            script = ("Add-Type -AssemblyName System.Speech; "
                      "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                      "$s.Speak([IO.File]::ReadAllText('%s', [Text.Encoding]::UTF8))" % esc)
            proc = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                     "-Command", script],
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._tts_proc = proc
            self._tts_text = text
            _ACTIVE_TTS.add(proc)
            self._set_status("正在朗读…（再点一次可停止）", "#0a7d0a")
        except Exception as exc:
            self._set_status("朗读不可用：%s" % exc, "#b06000")

    def stop_speak(self):
        """停止当前朗读（关窗口/关程序时也会调用）。"""
        _kill_proc(self._tts_proc)
        self._tts_proc = None
        self._tts_text = None

    def _clear_all(self):
        self.txt_in.delete("1.0", "end")
        self._set_output("")
        self._set_status("已清空")
        self.txt_in.focus_set()

    def _open_settings(self):
        self.app.open_settings()
        self._refresh_profile()

    def _refresh_profile(self):
        s = self.app.settings
        name = s.active_profile or "默认"
        if s.api_configured:
            self.lbl_profile.configure(
                text="当前接口：预设「%s」 · %s · %s" % (name, s.base_url, s.model),
                foreground="#666666")
        else:
            self.lbl_profile.configure(
                text="当前接口：预设「%s」尚未填写完整" % name, foreground="#b06000")

    # ==================================================================
    def destroy(self):
        self._closing = True
        self.stop_speak()          # 关窗口立刻停止后台朗读
        super().destroy()
