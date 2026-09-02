# -*- coding: utf-8 -*-
"""
翻译引擎：把 txt / json / md / csv / srt / log / text 文件用 AI 翻译成目标语言。

- AI 模式（OpenAI 兼容接口）：方向任意语言对，如 en2zh / zh2en / ja2zh / auto2zh…
- 内容按语言检测跳过“已是目标语言”的文本，避免重复翻译
- 进度回调按“AI 请求批次”推进，供界面显示实时进度条
"""
import csv
import io
import json
import os
import re
import time

import langs

# ---------------------------- 常量 / 正则 ----------------------------
SUPPORTED_EXTS = {".txt", ".json", ".md", ".csv", ".srt", ".log", ".text"}

_CJK_RE = re.compile("[\u4e00-\u9fff]")
_URL_RE = re.compile(r"^(https?://|ftp://|www\.)[^\s]+$", re.I)
_MAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
_WINPATH_RE = re.compile(r"^[A-Za-z]:[\\/].*")
_POSIXPATH_RE = re.compile(r"^/[\w.\-/]+$")
_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_VERSION_RE = re.compile(r"^\d+(\.\d+)+([.\-a-z0-9]*)$", re.I)
_NUM_RE = re.compile(r"^[\d.,+\-eE%¥$€£\u00a5]+$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_UNIT_RE = re.compile(r"^[\d.,]+\s?[a-zA-Z%]+$")
_SNAKE_RE = re.compile(r"^[a-z0-9]+([_-][a-z0-9]+)+$")
_CAMEL_RE = re.compile(r"^[a-z]+([A-Z][a-z]*)+$")

_BLANK_RE = re.compile(r"^\s*$")
_IDX_RE = re.compile(r"^\s*\d+\s*$")
_TIME_LINE_RE = re.compile(r"-->")


class Stats:
    """单文件翻译统计。"""

    def __init__(self):
        self.ai_calls = 0    # 实际发出的 AI 请求（批次）次数
        self.ai_units = 0    # 交给 AI 的单元数


class _CancelRun(Exception):
    pass


class Engine:
    """一次运行的翻译引擎（AI 模式）。"""

    def __init__(self, settings, ai=None,
                 log=None, cancel=None, progress=None):
        """
        settings:    Settings 实例（含 mode='ai' / direction 语言对）
        ai:          AIClient（必需）
        log:         log(msg) 回调
        cancel:      threading.Event，置位后尽快停止
        progress:    progress(done, total) 回调（按 AI 批次推进）
        """
        self.s = settings
        self.ai = ai
        self.log = log or (lambda m: None)
        self.cancel = cancel
        self.progress = progress
        self.mode = getattr(settings, "mode", "ai")
        self.direction = getattr(settings, "direction", "en2zh")  # 语言对，如 en2zh / ja2zh
        self.src_code, self.dst_code = langs.direction_pair(self.direction)
        self.pair_str = langs.pair_str(self.src_code, self.dst_code)
        self._ai_cache = {}      # 文本 -> 译文（会话级缓存）
        self._stats = Stats()
        self._file_ai_total = 0  # 当前文件的 AI 进度总量

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _maybe_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise _CancelRun()

    def _tick(self, done, total):
        if self.progress:
            self.progress(done, total)

    # ---- AI 进度：按“批”推进 ----
    def _begin_ai_phase(self, total):
        """告知本文件预计要翻译的唯一文本单元总数（进度条分母）。"""
        self._file_ai_total = total

    def _end_ai_phase(self):
        """收尾时把进度推满，防止因缓存/重复导致差一点不满。"""
        if self._file_ai_total:
            self._tick(self._file_ai_total, self._file_ai_total)
        self._file_ai_total = 0

    def _ai_tick(self):
        """每完成一个 AI 批次后推进进度（按已翻译单元数）。"""
        total = self._file_ai_total
        if total:
            done = min(self._stats.ai_units, total)
            self._tick(done, total)

    # ------------------------------------------------------------------
    # 文本语言判断
    # ------------------------------------------------------------------
    def _wants_translation(self, text):
        """
        按当前翻译语言对判断文本是否需要处理。
        只处理“检测为源语言”的行；已是目标语言的内容保留（避免重复翻译）。
        """
        if not text or not text.strip():
            return False
        det = langs.detect_language(text)
        if det is None:
            return False
        if self.src_code == "auto":
            # 已属于目标语言的内容不再翻译（如 自动识别→英 时跳过英文）
            return not langs.lang_matches_detected(self.dst_code, det)
        return langs.lang_matches_detected(self.src_code, det)

    # ------------------------------------------------------------------
    # 统一翻译一批唯一文本 -> {原文: 译文}
    # ------------------------------------------------------------------
    def _translate_map(self, unique_texts):
        """对需要翻译的唯一文本批量调用 AI，返回 {原文: 译文}。"""
        mapping = {}
        self._ai_translate(unique_texts, mapping)
        return mapping

    def _ai_translate(self, texts, mapping):
        """把 texts 中尚未翻译的唯一文本批量交给 AI，结果写入 mapping。"""
        need = []
        for t in texts:
            key = t.strip()
            if key and key not in self._ai_cache:
                need.append(key)
        unique = []
        seen = set()
        for t in need:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        for i in range(0, len(unique), 60):
            self._maybe_cancel()
            chunk = unique[i:i + 60]
            try:
                outs = self.ai.translate_units(chunk, self.pair_str)
            except _CancelRun:
                raise
            except Exception as exc:
                self.log("AI 翻译失败：%s" % exc)
                raise
            self._stats.ai_units += len(chunk)
            self._stats.ai_calls += self.ai.last_requests  # 该批实际成功请求数
            for src, dst in zip(chunk, outs):
                self._ai_cache[src] = (dst or "").strip()
            self._ai_tick()  # 每批完成后推进一次进度
        for t in texts:
            key = t.strip()
            if key in self._ai_cache:
                mapping[t] = self._ai_cache[key]
            else:
                mapping[t] = t
        return mapping

    # ------------------------------------------------------------------
    # 顶层入口：翻译一个文件
    # ------------------------------------------------------------------
    def translate_file(self, src, dst):
        """
        翻译文件 src，写入 dst；返回 Stats。
        出错会抛出异常（由调用方记录并继续下一个文件）。
        """
        self._stats = Stats()
        self._file_ai_total = 0
        t0 = time.time()
        self._maybe_cancel()
        ext = os.path.splitext(src)[1].lower()

        if ext == ".json":
            text_out = self._translate_json_file(src)
        elif ext == ".csv":
            text_out = self._translate_csv_file(src)
        elif ext == ".srt":
            text_out = self._translate_srt_file(src)
        else:
            text_out = self._translate_plain_file(src)

        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        with open(dst, "w", encoding=self.s.out_encoding, newline="") as f:
            f.write(text_out)
        self.log("已写入：%s（%.1fs，AI 请求 %d 次）"
                 % (os.path.basename(dst), time.time() - t0, self._stats.ai_calls))
        return self._stats

    # ------------------------------------------------------------------
    # 纯文本（txt / md / log / text）
    # ------------------------------------------------------------------
    def _translate_plain_file(self, src):
        text = _read_text(src)
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines()

        # 第一遍：收集唯一需要翻译的行
        todo = []      # 唯一文本
        seen = set()
        for ln in lines:
            if _BLANK_RE.match(ln):
                continue
            if self._wants_translation(ln):
                if ln not in seen:
                    seen.add(ln)
                    todo.append(ln)
        self._begin_ai_phase(len(todo))
        mapping = self._translate_map(todo)
        self._end_ai_phase()

        # 第二遍：逐行组装（进度已在翻译阶段按批推进）
        out = []
        for ln in lines:
            self._maybe_cancel()
            if _BLANK_RE.match(ln):
                out.append("")
            elif ln in mapping and mapping[ln] != ln:
                out.append(mapping[ln])
            else:
                out.append(ln)
        body = newline.join(out)
        if text.endswith("\r\n") or text.endswith("\n"):
            body += newline
        return body

    # ------------------------------------------------------------------
    # SRT 字幕：序号/时间轴保留，只翻译字幕文本
    # ------------------------------------------------------------------
    def _translate_srt_file(self, src):
        text = _read_text(src)
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines()
        blocks = []      # (起始行号, 文本行list) -> 只存文本串
        cur_text = []
        for ln in lines:
            if _BLANK_RE.match(ln) or _IDX_RE.match(ln) or _TIME_LINE_RE.search(ln):
                if cur_text:
                    blocks.append("\n".join(cur_text))
                    cur_text = []
            else:
                cur_text.append(ln)
        if cur_text:
            blocks.append("\n".join(cur_text))

        unique = []
        seen = set()
        for b in blocks:
            if self._wants_translation(b) and b not in seen:
                seen.add(b)
                unique.append(b)
        self._begin_ai_phase(len(unique))
        mapping = self._translate_map(unique)
        self._end_ai_phase()

        out = []
        cur_text = []

        def flush_text():
            if not cur_text:
                return
            unit = "\n".join(cur_text)
            if unit in mapping and mapping[unit] != unit:
                out.extend(mapping[unit].split("\n"))
            else:
                out.extend(cur_text)
            cur_text.clear()

        for ln in lines:
            if _BLANK_RE.match(ln) or _IDX_RE.match(ln) or _TIME_LINE_RE.search(ln):
                flush_text()
                out.append(ln)
            else:
                cur_text.append(ln)
        flush_text()
        body = newline.join(out)
        if text.endswith("\r\n") or text.endswith("\n"):
            body += newline
        return body

    # ------------------------------------------------------------------
    # JSON：保留结构，只处理字符串叶子；键名翻译为可选项
    # ------------------------------------------------------------------
    def _translate_json_file(self, src):
        raw = _read_text(src)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("JSON 解析失败：%s（已跳过）" % exc)

        # 收集唯一待译文本
        todo = []
        seen = set()

        def visit(node, path):
            if isinstance(node, dict):
                items = list(node.items())
                for k, v in items:
                    if self.s.json_translate_keys and isinstance(k, str) \
                            and self._json_leaf_wanted(k) and k not in seen:
                        seen.add(k)
                        todo.append(k)
                    visit(v, path + "/" + str(k))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    visit(v, path + "/%d" % i)
            elif isinstance(node, str) and self._json_leaf_wanted(node):
                if node not in seen:
                    seen.add(node)
                    todo.append(node)

        visit(obj, "")
        self._begin_ai_phase(len(todo))
        mapping = self._translate_map(todo)
        self._end_ai_phase()

        def rebuild(node, path):
            if isinstance(node, dict):
                newd = {}
                for k, v in node.items():
                    nk = mapping.get(k, k) if isinstance(k, str) else k
                    nv = rebuild(v, path + "/" + str(k))
                    newd[nk] = nv
                return newd
            if isinstance(node, list):
                return [rebuild(v, path + "/%d" % i) for i, v in enumerate(node)]
            if isinstance(node, str):
                return mapping.get(node, node)
            return node

        new_obj = rebuild(obj, "")
        indent = _json_indent(raw)
        if indent is None:
            return json.dumps(new_obj, ensure_ascii=False)
        return json.dumps(new_obj, ensure_ascii=False, indent=indent)

    def _json_leaf_wanted(self, s):
        if not s or not s.strip():
            return False
        if self.s.skip_code_like and _looks_code_like(s):
            return False
        return self._wants_translation(s)

    # ------------------------------------------------------------------
    # CSV：数据单元格翻译，首行表头默认保留
    # ------------------------------------------------------------------
    def _translate_csv_file(self, src):
        text = _read_text(src)
        delimiter = _detect_csv_delimiter(text)
        newline = "\r\n" if "\r\n" in text else "\n"
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        rows = [row for row in reader]
        if not rows:
            return text

        todo = []
        seen = set()
        for ri, row in enumerate(rows):
            if ri == 0 and self.s.csv_skip_header:
                continue
            for cell in row:
                if not cell or not cell.strip():
                    continue
                if self.s.skip_code_like and _looks_code_like(cell):
                    continue
                if self._wants_translation(cell) and cell not in seen:
                    seen.add(cell)
                    todo.append(cell)
        self._begin_ai_phase(len(todo))
        mapping = self._translate_map(todo)
        self._end_ai_phase()

        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row):
                if cell in mapping and mapping[cell] != cell:
                    row[ci] = mapping[cell]

        buf = io.StringIO(newline="")
        writer = csv.writer(buf, delimiter=delimiter, lineterminator=newline)
        writer.writerows(rows)
        return buf.getvalue()


# ======================================================================
# 模块级辅助函数
# ======================================================================
def _read_text(path):
    """按编码探测读取文本文件（utf-8-sig / utf-16 / gb18030）。"""
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _looks_code_like(s):
    """疑似 代码/路径/URL/邮件/纯数字/日期/ID 等不应翻译的文本。"""
    s = s.strip()
    if not s or len(s) > 500:
        return True
    if (_URL_RE.match(s) or _MAIL_RE.match(s) or _WINPATH_RE.match(s)
            or _POSIXPATH_RE.match(s)):
        return True
    if _UUID_RE.match(s) or _HEX_RE.match(s):
        return True
    if _DATE_RE.match(s) or _TIME_RE.match(s):
        return True
    if _NUM_RE.match(s) or _UNIT_RE.match(s) or _VERSION_RE.match(s):
        return True
    # 无空格、含下划线/连字符/数字/驼峰：多半是代码标识符
    if " " not in s:
        if _SNAKE_RE.match(s) or _CAMEL_RE.match(s):
            return True
        if re.search(r"\d", s) and not _CJK_RE.search(s):
            return True
    if re.search(r"[{}<>\[\]]", s):
        return True
    return False


def _detect_csv_delimiter(text):
    sample = "\n".join(text.splitlines()[:5])
    best, best_n = ",", 0
    for d in (",", ";", "\t", "|"):
        n = sample.count(d)
        if n > best_n:
            best, best_n = d, n
    return best


def _json_indent(raw):
    """猜测原 JSON 缩进：有缩进返回空格数；紧凑则返回 None。"""
    for line in raw.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and line != stripped and stripped[0] not in "}]":
            return len(line) - len(stripped)
    return None
