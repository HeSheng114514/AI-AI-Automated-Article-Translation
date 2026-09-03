# -*- coding: utf-8 -*-
"""
桌面图形界面（tkinter）：AI 批量翻译工具。

仅提供 AI 翻译模式（OpenAI 兼容接口），支持多语言方向；
翻译过程中按 AI 请求批次实时刷新进度条。
"""
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

import config
import langs
from ai_client import AIClient, AIError
from config import Settings
from engine import Engine, SUPPORTED_EXTS

OUT_DIR_NAME = "翻译结果"

_RUNNING = "翻译中…"
_DONE = "✓ 完成"
_FAIL = "✗ 失败"
_TAG_COLORS = {_DONE: "green", _FAIL: "red", _RUNNING: "blue", "": "black"}


def run():
    root = tk.Tk()
    TranslatorApp(root)
    root.mainloop()


class TranslatorApp:
    def __init__(self, root):
        self.root = root
        root.title("AI 批量翻译工具")
        root.geometry("960x700")
        root.minsize(860, 620)

        self.settings = Settings.load()
        self.settings.ensure_initial_profile()

        self.files = []            # [{path, rel}]
        self.worker = None
        self.cancel_evt = threading.Event()
        self.q = queue.Queue()

        self._build_ui()
        self._apply_settings_to_ui()
        self._poll_queue()

    # ==================================================================
    # 界面构建
    # ==================================================================
    def _build_ui(self):
        pad = dict(padx=8, pady=4)
        f = ttk.Frame(self.root, padding=8)
        f.pack(fill="both", expand=True)

        # --- 1) 输入文件列表 -------------------------------------------
        box1 = ttk.LabelFrame(f, text="① 选择要翻译的文件 / 文件夹", padding=6)
        box1.pack(fill="both", expand=True, **pad)

        btns = ttk.Frame(box1)
        btns.pack(fill="x")
        ttk.Button(btns, text="＋ 添加文件…", command=self.add_files).pack(side="left")
        ttk.Button(btns, text="＋ 添加文件夹…", command=self.add_folder).pack(side="left", padx=4)
        ttk.Button(btns, text="移除选中", command=self.remove_selected).pack(side="left")
        ttk.Button(btns, text="清空列表", command=self.clear_files).pack(side="left", padx=4)
        self.var_recursive = tk.BooleanVar(value=self.settings.recursive)
        ttk.Checkbutton(btns, text="包含子文件夹", variable=self.var_recursive).pack(side="left", padx=8)
        ttk.Label(btns, text="支持：txt / json / md / csv / srt / log / text").pack(side="right")

        cols = ("path", "fmt", "status")
        self.tree = ttk.Treeview(box1, columns=cols, show="headings", height=8)
        self.tree.heading("path", text="文件路径")
        self.tree.heading("fmt", text="格式")
        self.tree.heading("status", text="状态")
        self.tree.column("path", width=620)
        self.tree.column("fmt", width=70, anchor="center")
        self.tree.column("status", width=110, anchor="center")
        for tag, color in _TAG_COLORS.items():
            self.tree.tag_configure(tag, foreground=color)
        self.tree.pack(fill="both", expand=True, pady=(6, 0))

        # --- 2) 输出目录 ------------------------------------------------
        box2 = ttk.LabelFrame(f, text="② 输出目录", padding=6)
        box2.pack(fill="x", **pad)
        row = ttk.Frame(box2)
        row.pack(fill="x")
        self.var_out = tk.StringVar()
        ttk.Entry(row, textvariable=self.var_out).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", command=self.choose_outdir).pack(side="left", padx=6)
        ttk.Label(row, text="（空则自动放在源目录下的“%s”文件夹）" % OUT_DIR_NAME).pack(side="left")

        # --- 3) AI 翻译设置 --------------------------------------------
        box3 = ttk.LabelFrame(f, text="③ AI 翻译设置", padding=6)
        box3.pack(fill="x", **pad)
        row3 = ttk.Frame(box3)
        row3.pack(fill="x")

        ttk.Label(row3, text="模式：AI 翻译（OpenAI 兼容接口）").pack(side="left")

        sep = ttk.Separator(row3, orient="vertical")
        sep.pack(side="left", fill="y", padx=10)

        g2 = ttk.Frame(row3)
        g2.pack(side="left")
        ttk.Label(g2, text="源语言").grid(row=0, column=0, padx=(0, 2))
        self.var_src = tk.StringVar()
        self.cb_src = ttk.Combobox(g2, textvariable=self.var_src, state="readonly",
                                   values=langs.source_language_options(), width=10)
        self.cb_src.grid(row=0, column=1)
        ttk.Label(g2, text="→").grid(row=0, column=2, padx=6)
        ttk.Label(g2, text="目标语言").grid(row=0, column=3, padx=(0, 2))
        self.var_dst = tk.StringVar()
        self.cb_dst = ttk.Combobox(g2, textvariable=self.var_dst, state="readonly",
                                   values=langs.target_language_options(), width=10)
        self.cb_dst.grid(row=0, column=4)
        ttk.Label(g2, text="可任意组合，如 英→韩、日→越、自动识别→英",
                  foreground="#666666").grid(row=1, column=0, columnspan=5, sticky="w", pady=(2, 0))

        g3 = ttk.Frame(row3)
        g3.pack(side="left", padx=(18, 0))
        self.var_json_keys = tk.BooleanVar(value=self.settings.json_translate_keys)
        ttk.Checkbutton(g3, text="JSON 键名也翻译（默认只翻值）",
                        variable=self.var_json_keys).grid(row=0, column=0, sticky="w")
        self.var_skip = tk.BooleanVar(value=self.settings.skip_code_like)
        ttk.Checkbutton(g3, text="跳过疑似代码/路径/URL/数字的文本",
                        variable=self.var_skip).grid(row=1, column=0, sticky="w")

        self.lbl_api_hint = ttk.Label(
            f, text="还没有配置接口？请点右下角【设置…】填写 Base URL / API Key / 模型，并可用“测试连接”验证。",
            foreground="#b06000")
        self.lbl_api_hint.pack(fill="x", padx=8, pady=(0, 2))

        # --- 4) 底部按钮、进度条与日志 -----------------------------------
        box4 = ttk.Frame(f)
        box4.pack(fill="x", **pad)
        self.btn_start = ttk.Button(box4, text="▶ 开始翻译", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(box4, text="■ 停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(box4, text="打开输出目录", command=self.open_outdir).pack(side="left")
        ttk.Button(box4, text="设置…", command=self.open_settings).pack(side="right")
        ttk.Button(box4, text="帮助", command=self.open_help).pack(side="right", padx=6)

        self.progress = ttk.Progressbar(f, mode="determinate")
        self.progress.pack(fill="x", **pad)
        self.lbl_status = ttk.Label(f, text="AI：未配置")
        self.lbl_status.pack(fill="x", padx=8)

        self.log_txt = scrolledtext.ScrolledText(f, height=10, state="disabled",
                                                 font=("Microsoft YaHei UI", 9))
        self.log_txt.pack(fill="both", expand=True, **pad)

    # ==================================================================
    # UI <-> 设置 同步
    # ==================================================================
    def _current_pair(self):
        """当前两个下拉框的语言码，如 ('ja','vi') / ('auto','zh')。"""
        src = langs.display_code(self.var_src.get())
        dst = langs.display_code(self.var_dst.get())
        return src, dst

    def _current_pair_str(self):
        return langs.pair_str(*self._current_pair())

    def _apply_settings_to_ui(self):
        self.var_json_keys.set(self.settings.json_translate_keys)
        self.var_skip.set(self.settings.skip_code_like)
        self.var_recursive.set(self.settings.recursive)
        # 从保存的语言对恢复两个下拉框
        src, dst = langs.direction_pair(self.settings.direction)
        self.var_src.set(langs.display_name(src))
        self.var_dst.set(langs.display_name(dst))
        self._refresh_state_labels()

    def _refresh_state_labels(self):
        d = self.settings
        if d.api_configured:
            txt = "AI：已配置  %s  ·  模型 %s" % (d.base_url, d.model)
            self.lbl_api_hint.configure(text="")
        else:
            txt = "AI：未配置（请到【设置】填写 Base URL / API Key / 模型）"
            self.lbl_api_hint.configure(
                text="还没有配置接口？点右下角【设置…】填写 Base URL / API Key / 模型，并用“测试连接”验证。")
        self.lbl_status.configure(text="状态：" + txt)

    # ==================================================================
    # 文件列表操作
    # ==================================================================
    def add_files(self):
        exts = " ".join("*" + e for e in sorted(SUPPORTED_EXTS))
        paths = filedialog.askopenfilenames(
            title="选择文件（支持 txt/json/md/csv/srt/log/text）",
            filetypes=[("文本与数据文件", exts), ("所有文件", "*.*")])
        if not paths:
            return
        try:
            base = os.path.commonpath([os.path.abspath(p) for p in paths])
        except ValueError:
            base = os.path.dirname(os.path.abspath(paths[0]))
        if len(paths) == 1:
            base = os.path.dirname(os.path.abspath(paths[0]))
        for p in paths:
            rel = os.path.basename(p) if len(paths) == 1 else os.path.relpath(p, base)
            self._append_file(p, rel)
        self._auto_outdir(base)

    def add_folder(self):
        folder = filedialog.askdirectory(title="选择文件夹（将递归翻译其中的文本文件）")
        if not folder:
            return
        folder = os.path.abspath(folder)
        added = 0
        for dirpath, dirnames, filenames in os.walk(folder):
            if not self.var_recursive.get():
                dirnames[:] = []
            dirnames[:] = [dn for dn in dirnames
                           if not (OUT_DIR_NAME in dn and os.path.join(dirpath, dn) != folder)]
            for fn in sorted(filenames):
                ext = os.path.splitext(fn)[1].lower()
                if ext not in SUPPORTED_EXTS:
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, folder)
                self._append_file(full, rel)
                added += 1
        if added == 0:
            messagebox.showinfo("添加文件夹",
                                "该文件夹内没有找到支持的文本文件。\n支持格式：txt / json / md / csv / srt / log / text")
            return
        self._auto_outdir(folder)

    def _append_file(self, path, rel):
        self.files.append({"path": os.path.abspath(path), "rel": rel})
        ext = os.path.splitext(path)[1].lstrip(".").upper() or "?"
        self.tree.insert("", "end", values=(path, ext, ""), tags=("",))

    def _auto_outdir(self, base):
        if not self.var_out.get().strip():
            self.var_out.set(os.path.join(base, OUT_DIR_NAME))

    def remove_selected(self):
        for item in self.tree.selection():
            idx = self.tree.index(item)
            self.tree.delete(item)
            if 0 <= idx < len(self.files):
                del self.files[idx]

    def clear_files(self):
        self.tree.delete(*self.tree.get_children())
        self.files.clear()

    def choose_outdir(self):
        cur = self.var_out.get().strip()
        d = filedialog.askdirectory(title="选择输出目录", initialdir=cur or None)
        if d:
            self.var_out.set(os.path.abspath(d))

    def open_outdir(self):
        d = self.var_out.get().strip()
        if d and os.path.isdir(d):
            os.startfile(d)  # noqa
        else:
            messagebox.showinfo("输出目录", "输出目录尚未生成，请先运行一次翻译。")

    def open_help(self):
        readme = os.path.join(config.APP_DIR, "README使用说明.md")
        if os.path.isfile(readme):
            os.startfile(readme)  # noqa
        else:
            messagebox.showinfo("帮助", "未找到说明文件 README使用说明.md")

    # ==================================================================
    # 翻译主流程
    # ==================================================================
    def start(self):
        if self.worker and self.worker.is_alive():
            return

        if not self.files:
            messagebox.showwarning("开始翻译", "请先选择要翻译的文件或文件夹。")
            return
        for fi in self.files:
            if not os.path.isfile(fi["path"]):
                messagebox.showwarning("开始翻译", "文件不存在：\n%s" % fi["path"])
                return
        if not self.settings.api_configured:
            messagebox.showerror("AI 模式",
                                 "尚未填写完整的 AI 接口信息。\n请先到【设置】填写 Base URL / API Key / 模型。")
            return

        outdir = self.var_out.get().strip()
        if not outdir:
            outdir = os.path.join(os.path.dirname(self.files[0]["path"]), OUT_DIR_NAME)
            self.var_out.set(outdir)
        outdir = os.path.abspath(outdir)

        # 快照
        src_code, dst_code = self._current_pair()
        if src_code != "auto" and src_code == dst_code:
            messagebox.showwarning("语言相同",
                                   "源语言与目标语言相同（%s → %s），无需翻译。\n请重新选择。" % (
                                       langs.display_name(src_code), langs.display_name(dst_code)))
            return
        snap = Settings(self.settings.as_dict())
        snap.mode = "ai"
        snap.direction = langs.pair_str(src_code, dst_code)
        snap.json_translate_keys = self.var_json_keys.get()
        snap.skip_code_like = self.var_skip.get()
        snap.recursive = self.var_recursive.get()

        # 持久化界面选项
        self.settings.direction = snap.direction
        self.settings.json_translate_keys = snap.json_translate_keys
        self.settings.skip_code_like = snap.skip_code_like
        self.settings.recursive = snap.recursive
        self.settings.save()

        # 结果路径映射（防止覆盖原文件）
        jobs = []
        src_set = {os.path.normcase(fi["path"]) for fi in self.files}
        for fi in self.files:
            rel = fi["rel"] or os.path.basename(fi["path"])
            dst = os.path.normpath(os.path.join(outdir, rel))
            if os.path.normcase(dst) in src_set:
                messagebox.showerror("输出冲突",
                                     "输出文件与源文件相同，可能覆盖原文件！\n请换一个输出目录。\n\n%s" % dst)
                return
            jobs.append((fi["path"], dst))

        self.cancel_evt.clear()
        self.progress.configure(maximum=len(jobs), value=0)
        self._set_running(True)
        self.log("=" * 60)
        self.log("开始 AI 翻译：%d 个文件  |  %s" % (
            len(jobs), langs.pair_display(src_code, dst_code)))
        threading.Thread(target=self._worker, args=(jobs, outdir, snap), daemon=True).start()

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.cancel_evt.set()
            self.log("正在停止…（等待当前批次结束）")

    def _set_running(self, running):
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")

    # ------------------------------------------------------------------
    def _worker(self, jobs, outdir, snap):
        from engine import _CancelRun
        ai = AIClient(snap.base_url, snap.api_key, snap.model,
                      timeout=snap.ai_timeout, log=lambda m: self._enqueue(("log", m)))
        idx_cell = [0]

        def on_progress(done, total):
            # done/total 是该文件内的进度；进度条以文件为整格、内部按批次细分
            self._enqueue(("prog", idx_cell[0], done, total))

        engine = Engine(snap, ai=ai,
                        log=lambda m: self._enqueue(("log", m)),
                        cancel=self.cancel_evt, progress=on_progress)
        ok, fail = 0, 0
        ai_units, ai_calls = 0, 0
        t_start = time_now()

        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as exc:
            self._enqueue(("log", "无法创建输出目录：%s" % exc))
            self._enqueue(("finish", ok, fail, ai_units, ai_calls, t_start))
            return

        for i, (src, dst) in enumerate(jobs):
            if self.cancel_evt.is_set():
                self._enqueue(("log", "用户已停止。"))
                break
            idx_cell[0] = i
            self._enqueue(("file_start", i, len(jobs), src))
            try:
                stats = engine.translate_file(src, dst)
                ok += 1
                ai_units += stats.ai_units
                ai_calls += stats.ai_calls
                self._enqueue(("file_done", src, stats))
            except _CancelRun:
                self._enqueue(("log", "用户已停止。"))
                break
            except Exception as exc:
                fail += 1
                self._enqueue(("file_error", src, str(exc)))
            self._enqueue(("file_progress", i + 1))
        self._enqueue(("finish", ok, fail, ai_units, ai_calls, t_start))

    # ==================================================================
    # 线程 -> UI 消息
    # ==================================================================
    def _enqueue(self, item):
        self.q.put(item)

    def _poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                self._handle(item)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle(self, item):
        kind = item[0]
        if kind == "log":
            self.log(item[1])
        elif kind == "file_start":
            self._set_item_by_path(item[3], _RUNNING)
            self.log("▶ 翻译中 (%d/%d)：%s" % (item[1] + 1, item[2], os.path.basename(item[3])))
        elif kind == "prog":
            idx, done, total = item[1], item[2], item[3]
            if total and total > 0:
                frac = min(1.0, float(done) / total)
                self.progress.configure(value=idx + frac)
        elif kind == "file_done":
            self._set_item_by_path(item[1], _DONE)
            st = item[2]
            self.log("   ✓ 完成：AI 单元 %d（请求 %d 次）" % (st.ai_units, st.ai_calls))
        elif kind == "file_error":
            self._set_item_by_path(item[1], _FAIL)
            self.log("   ✗ %s 失败：%s" % (os.path.basename(item[1]), item[2]))
        elif kind == "file_progress":
            self.progress.configure(value=item[1])
        elif kind == "finish":
            self._on_finish(*item[1:])

    def _on_finish(self, ok, fail, ai_units, ai_calls, t_start):
        self._set_running(False)
        secs = max(0, time_now() - t_start)
        self.log("-" * 60)
        self.log("全部结束：成功 %d 个，失败 %d 个，AI 单元 %d，请求 %d 次，用时 %.1f 秒。"
                 % (ok, fail, ai_units, ai_calls, secs))
        if ok:
            self.log("输出目录：%s" % self.var_out.get().strip())
        if fail == 0 and ok:
            messagebox.showinfo("完成",
                                "翻译完成：成功 %d 个文件。\n输出目录：\n%s"
                                % (ok, self.var_out.get().strip()))
        elif fail:
            messagebox.showwarning("完成（部分失败）",
                                   "成功 %d 个，失败 %d 个，详见日志。\n输出目录：\n%s"
                                   % (ok, fail, self.var_out.get().strip()))

    # ==================================================================
    # 设置窗口
    # ==================================================================
    def open_settings(self):
        SettingsDialog(self)

    # ==================================================================
    # 其它
    # ==================================================================
    def log(self, msg):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", msg + "\n")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def _set_item_by_path(self, path, status):
        for item in self.tree.get_children():
            values = self.tree.item(item, "values")
            if values and os.path.normcase(values[0]) == os.path.normcase(path):
                tag = {_DONE: "green", _FAIL: "red", _RUNNING: "blue"}.get(status, "")
                self.tree.set(item, "status", status)
                self.tree.item(item, tags=(tag,))
                break


def time_now():
    import time
    return time.time()


# ======================================================================
# 设置对话框
# ======================================================================
class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.s = app.settings
        self.title("设置 - AI 接口")
        # 不写死窗口大小：先按内容自动测量，再设置足够大的窗口，避免底部按钮被裁切
        self.transient(app.root)
        self.grab_set()
        self.resizable(True, True)
        self.minsize(620, 200)

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)

        # ---------- 接口预设（多账号一键切换）----------
        preset_box = ttk.LabelFrame(body, text="接口预设（可保存多组 API Key / 模型，一键切换）", padding=8)
        preset_box.pack(fill="x")
        row_p = ttk.Frame(preset_box)
        row_p.pack(fill="x")
        ttk.Label(row_p, text="预设：").pack(side="left")
        self.var_preset = tk.StringVar()
        self.cb_preset = ttk.Combobox(row_p, textvariable=self.var_preset, state="readonly",
                                      values=self.s.profile_names(), width=18)
        self.cb_preset.pack(side="left")
        self.cb_preset.bind("<<ComboboxSelected>>", lambda e: self._on_preset_selected())
        ttk.Button(row_p, text="＋ 新建预设…", command=self._new_preset).pack(side="left", padx=(8, 0))
        ttk.Button(row_p, text="保存到当前预设", command=self._save_current_to_preset).pack(side="left", padx=4)
        ttk.Button(row_p, text="删除预设", command=self._delete_preset).pack(side="left")
        ttk.Label(preset_box,
                  text="下拉选择预设即一键切换该组的 Base URL / API Key / 模型；可直接编辑下方三项后保存。",
                  foreground="#666666").pack(fill="x", pady=(4, 0))

        # ---------- AI 接口 ----------
        ai_box = ttk.LabelFrame(body, text="当前预设的接口参数", padding=8)
        ai_box.pack(fill="x", pady=(10, 0))

        self.var_url = tk.StringVar(value=self.s.base_url)
        self.var_key = tk.StringVar(value=self.s.api_key)
        self.var_model = tk.StringVar(value=self.s.model)

        ttk.Label(ai_box, text="Base URL：").grid(row=0, column=0, sticky="e", pady=2)
        ttk.Entry(ai_box, textvariable=self.var_url, width=60).grid(row=0, column=1, sticky="w")
        ttk.Label(ai_box, text="例如 DeepSeek：https://api.deepseek.com/v1\n"
                               "          OpenAI：https://api.openai.com/v1",
                  foreground="#666666").grid(row=0, column=2, rowspan=1, sticky="w", padx=8)

        ttk.Label(ai_box, text="API Key：").grid(row=1, column=0, sticky="e", pady=2)
        self.key_entry = ttk.Entry(ai_box, textvariable=self.var_key, width=60, show="•")
        self.key_entry.grid(row=1, column=1, sticky="w")
        ttk.Button(ai_box, text="显示", width=5, command=self._toggle_key).grid(row=1, column=2, sticky="w", padx=8)

        ttk.Label(ai_box, text="模型：").grid(row=2, column=0, sticky="e", pady=2)
        ttk.Entry(ai_box, textvariable=self.var_model, width=60).grid(row=2, column=1, sticky="w")
        ttk.Label(ai_box, text="例如 deepseek-chat / gpt-4o-mini",
                  foreground="#666666").grid(row=2, column=2, sticky="w", padx=8)

        self.lbl_api_state = ttk.Label(ai_box, text="", foreground="#0a7d0a")
        self.lbl_api_state.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ---------- 输出编码 ----------
        out_box = ttk.LabelFrame(body, text="输出文件编码", padding=8)
        out_box.pack(fill="x", pady=(10, 0))
        self.var_enc = tk.StringVar(value=self.s.out_encoding)
        labels = list(config._ENC_LABELS.items())
        for i, (code, name) in enumerate(labels):
            ttk.Radiobutton(out_box, text="%s（%s）" % (name, code),
                            value=code, variable=self.var_enc).grid(row=0, column=i, sticky="w", padx=6)
        ttk.Label(out_box, text="若用 Excel 打开 CSV 出现乱码，请选“UTF-8（含 BOM）”。",
                  foreground="#666666").grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # ---------- 底部 ----------
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(16, 0))
        ttk.Button(btns, text="测试连接", command=self._test_connection).pack(side="left")
        ttk.Button(btns, text="保存并关闭", command=self._save_and_close).pack(side="right")
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=6)

        self._refresh_api_state()

        # 预设下拉显示当前激活预设；随后按内容自动设定窗口大小（防止按钮被裁切）
        self._refresh_preset_box()
        self.update_idletasks()
        w = max(680, min(self.winfo_reqwidth() + 30, 1024))
        h = self.winfo_reqheight() + 26   # 预留标题栏等空间
        self.geometry("%dx%d" % (w, h))

    # ------------------------------------------------------------------
    # 接口预设（多账号一键切换）
    # ------------------------------------------------------------------
    def _fields(self):
        return {
            "base_url": self.var_url.get().strip(),
            "api_key": self.var_key.get().strip(),
            "model": self.var_model.get().strip(),
        }

    def _dirty(self):
        """输入框内容与当前预设是否不一致（有未保存修改）。"""
        p = self.s.get_profile(self.s.active_profile)
        if p is None:
            return False
        f = self._fields()
        return any(p.get(k, "") != f[k] for k in ("base_url", "api_key", "model"))

    def _load_preset_into_fields(self, name):
        p = self.s.get_profile(name)
        if p is None:
            return
        self.var_url.set(p.get("base_url", ""))
        self.var_key.set(p.get("api_key", ""))
        self.var_model.set(p.get("model", ""))

    def _mirror_from_active(self):
        """把激活预设同步到顶层 base_url/api_key/model（主界面/引擎读取的字段）。"""
        p = self.s.get_profile(self.s.active_profile)
        if p is not None:
            self.s.base_url = p.get("base_url", "")
            self.s.api_key = p.get("api_key", "")
            self.s.model = p.get("model", "")

    def _refresh_preset_box(self):
        names = self.s.profile_names()
        self.cb_preset.configure(values=names)
        cur = self.var_preset.get()
        if cur not in names:
            cur = self.s.active_profile if self.s.active_profile in names else (names[0] if names else "")
            self.var_preset.set(cur)

    def _commit(self, log_msg=None):
        """写入 config.json 并刷新主界面状态栏。"""
        self.s.save()
        self.app._refresh_state_labels()
        if log_msg:
            self.app.log(log_msg)

    def _on_preset_selected(self):
        """一键切换：选中下拉项即切换为该组的 Key/模型。"""
        name = self.var_preset.get()
        if not name:
            return
        old = self.s.active_profile
        if old and old != name and self._dirty():
            keep = messagebox.askyesno(
                "未保存的修改",
                "预设「%s」里有尚未保存的修改。\n是否先保存到该预设再切换？\n（选“否”将丢弃修改并直接切换）" % old)
            if keep:
                self.s.set_profile_fields(old, **self._fields())
                self._commit()
        self.s.active_profile = name
        self._load_preset_into_fields(name)
        self._mirror_from_active()
        self._refresh_api_state()
        self._commit("已切换到预设：%s" % name)

    def _new_preset(self):
        taken = set(self.s.profile_names())
        i = len(taken) + 1
        suggest = "预设%d" % i
        while suggest in taken:
            i += 1
            suggest = "预设%d" % i
        name = simpledialog.askstring("新建预设", "请输入预设名称（例如 DeepSeek、OpenAI、通义）：",
                                      initialvalue=suggest, parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if name in self.s.profile_names():
            messagebox.showwarning("新建预设", "已存在同名预设：%s" % name)
            return
        self.s.api_profiles.append({"name": name, **self._fields()})
        self.s.active_profile = name
        self._mirror_from_active()
        self._refresh_preset_box()
        self._refresh_api_state()
        self._commit("已新建预设：%s" % name)

    def _delete_preset(self):
        name = self.var_preset.get()
        if not name or len(self.s.profile_names()) <= 1:
            messagebox.showwarning("删除预设", "至少需要保留一个预设，不能删除。")
            return
        if not messagebox.askyesno("删除预设", "确定删除预设「%s」？" % name):
            return
        self.s.api_profiles = [p for p in self.s.api_profiles if p.get("name") != name]
        self.s.active_profile = self.s.profile_names()[0]
        self._refresh_preset_box()
        self._load_preset_into_fields(self.s.active_profile)
        self._mirror_from_active()
        self._refresh_api_state()
        self._commit("已删除预设：%s" % name)

    def _save_current_to_preset(self):
        """把当前输入框内容覆盖保存到当前选中的预设。"""
        name = self.var_preset.get() or self.s.active_profile
        if not name:
            messagebox.showwarning("保存到预设", "请先选择预设。")
            return
        self.s.set_profile_fields(name, **self._fields())
        self._refresh_api_state()
        self._commit("已保存到预设：%s" % name)

    # ------------------------------------------------------------------
    def _toggle_key(self):
        if self.key_entry.cget("show"):
            self.key_entry.configure(show="")
        else:
            self.key_entry.configure(show="•")

    def _sync_fields(self):
        self.s.base_url = self.var_url.get().strip()
        self.s.api_key = self.var_key.get().strip()
        self.s.model = self.var_model.get().strip()
        self.s.out_encoding = self.var_enc.get()

    def _refresh_api_state(self):
        f = self._fields()
        name = self.var_preset.get() or self.s.active_profile or "默认"
        if f["base_url"] and f["api_key"] and f["model"]:
            self.lbl_api_state.configure(
                text="✓ 预设「%s」已填写 Base URL / API Key / 模型" % name, foreground="#0a7d0a")
        else:
            self.lbl_api_state.configure(
                text="预设「%s」：三项都填写后即可开始 AI 翻译" % name, foreground="#888888")

    def _test_connection(self):
        self._sync_fields()
        f = self._fields()
        if not (f["base_url"] and f["api_key"] and f["model"]):
            messagebox.showwarning("测试连接", "请先填写 Base URL / API Key / 模型。")
            return
        self.lbl_api_state.configure(text="正在请求模型…", foreground="#aa6600")
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _test_worker(self):
        s = self.s
        client = AIClient(s.base_url, s.api_key, s.model, timeout=s.ai_timeout)
        try:
            reply = client.test()
        except AIError as exc:
            self.app.root.after(0, lambda: self.lbl_api_state.configure(
                text="测试失败：%s" % exc, foreground="#cc0000"))
            return
        except Exception as exc:
            self.app.root.after(0, lambda: self.lbl_api_state.configure(
                text="测试失败：%s" % exc, foreground="#cc0000"))
            return
        self.app.root.after(0, lambda: self.lbl_api_state.configure(
            text="✓ 测试成功！模型回复：%s" % reply.strip()[:60], foreground="#0a7d0a"))

    # ------------------------------------------------------------------
    def _save_and_close(self):
        self.s.out_encoding = self.var_enc.get()
        name = self.var_preset.get() or self.s.active_profile
        if not name or name not in self.s.profile_names():
            name = self.s.profile_names()[0] if self.s.profile_names() else "默认"
        self.s.set_profile_fields(name, **self._fields())
        self._commit()
        self.app._apply_settings_to_ui()
        self.app.log("设置已保存（当前预设：%s）" % name)
        self.destroy()
