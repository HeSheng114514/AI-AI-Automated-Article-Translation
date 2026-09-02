# -*- coding: utf-8 -*-
"""
AI 客户端：OpenAI 兼容 Chat Completions 接口（纯标准库实现）。
可用于 OpenAI / DeepSeek / 通义千问 / 智谱 / Moonshot 等兼容服务。
"""
import json
import re
import time
import urllib.error
import urllib.request

import langs

_MARKER_RE = re.compile(r"<<<(\d+)>>>\s*(.*?)(?=<<<\d+>>>|\Z)", re.S)


class AIError(Exception):
    """AI 调用失败（含用户可读的中文提示）。"""


def _friendly_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        hint = {
            400: "请求格式错误 (400)",
            401: "API Key 无效或已过期 (401)，请在设置中检查",
            403: "无访问权限 (403)，请检查账号额度/权限",
            404: "接口地址不存在 (404)，请检查 Base URL（应以 /v1 结尾）",
            429: "请求过于频繁或额度不足 (429)",
        }.get(code, "HTTP %d" % code)
        if body:
            hint += " | %s" % body
        return AIError(hint)
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return AIError("无法连接服务器，请检查网络与 Base URL：%s" % reason)
    return AIError(str(exc))


class AIClient:
    def __init__(self, base_url, api_key, model, timeout=90, log=None):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = timeout
        self.log = log or (lambda msg: None)
        self.last_requests = 0   # 最近一次 translate_units 实际发起的请求数

    # ------------------------------------------------------------------
    def chat(self, messages, temperature=0.3):
        """发送一次对话，返回 assistant 文本；失败抛 AIError。"""
        if not self.api_key:
            raise AIError("尚未填写 API Key，请先在 设置 中填写")
        if not self.model:
            raise AIError("尚未填写模型名称，请先在 设置 中填写")
        url = self.base_url + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.api_key,
        }
        last_err = None
        for attempt in range(4):  # 最多重试 4 次
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                obj = json.loads(raw)
                content = obj["choices"][0]["message"]["content"]
                self.last_requests += 1
                return (content or "").strip()
            except urllib.error.HTTPError as exc:
                last_err = _friendly_error(exc)
                if exc.code in (400, 401, 403, 404):
                    break  # 不可重试
            except urllib.error.URLError as exc:
                last_err = _friendly_error(exc)
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                last_err = AIError("接口返回无法解析：%s" % exc)
            except Exception as exc:
                last_err = _friendly_error(exc)
            if attempt < 3:
                wait = 2 ** attempt
                self.log("接口调用失败，%d 秒后重试：%s" % (wait, last_err))
                time.sleep(wait)
        raise last_err if last_err else AIError("未知错误")

    # ------------------------------------------------------------------
    def test(self):
        """连接测试：请求模型回复 OK。"""
        reply = self.chat([{"role": "user", "content": "只回复两个字母：OK"}], temperature=0)
        return reply

    # ------------------------------------------------------------------
    def translate_units(self, units, direction):
        """
        批量翻译多个文本单元（保持顺序）。
        direction: 语言对，如 'en2zh' / 'zh2en' / 'ja2zh' / 'ko2zh' / 'auto2zh'…
        返回与 units 等长列表。内部自动分批、失败逐段二分重试。
        """
        result = []
        bucket = []
        bucket_chars = 0
        self.last_requests = 0
        for u in units:
            if not u.strip():
                result.append(u)  # 空单元原样保留
                continue
            if bucket and (len(bucket) >= 10 or bucket_chars + len(u) > 1400):
                result.extend(self._translate_bucket(bucket, direction))
                bucket, bucket_chars = [], 0
            bucket.append(u)
            bucket_chars += len(u)
        if bucket:
            result.extend(self._translate_bucket(bucket, direction))
        return result

    # ------------------------------------------------------------------
    def _prompt_meta(self, direction):
        """从方向码解析 源语言/目标语言 的提示用语。"""
        src, dst = langs.direction_pair(direction)
        target = langs.target_language_name(dst)
        if src != "auto":
            src_name = langs.target_language_name(src)
            return ("Translate the %s text into %s." % (src_name, target), target)
        return ("Translate the following text into %s." % target, target)

    # ------------------------------------------------------------------
    def _translate_bucket(self, units, direction):
        if len(units) == 1:
            return [self._translate_single(units[0], direction)]

        meta, _target = self._prompt_meta(direction)
        parts = []
        for i, u in enumerate(units, 1):
            parts.append("<<<%d>>>\n%s" % (i, u))
        marked = "\n".join(parts)
        messages = [
            {"role": "system", "content": (
                "You are a professional translator. %s Translate each numbered segment. "
                "Keep numbers, code, URLs and markup unchanged. "
                "Reply ONLY with the translations, keeping every <<<N>>> marker line "
                "exactly as given, in the same order, one segment per marker." % meta
            )},
            {"role": "user", "content": marked},
        ]
        raw = self.chat(messages)
        found = {}
        for num, seg in _MARKER_RE.findall(raw):
            try:
                found[int(num)] = seg.strip()
            except ValueError:
                pass

        # 全部命中 -> 按序返回
        if all(i in found for i in range(1, len(units) + 1)):
            return [found[i] for i in range(1, len(units) + 1)]

        # 部分命中 -> 对未命中的区间二分后递归，保证正确性
        self.log("分段标记未完整返回，改为逐段翻译（本批 %d 段）" % len(units))
        out = []
        for i, u in enumerate(units, 1):
            if i in found:
                out.append(found[i])
            else:
                out.append(self._translate_single(u, direction))
        return out

    def _translate_single(self, unit, direction):
        meta, _target = self._prompt_meta(direction)
        text = unit.strip()
        # 超长单段按句子拆分，避免超限
        if len(text) > 1800:
            sentences = re.split(r"(?<=[.!?。])\s*", text)
            if len(sentences) > 1:
                return " ".join(self.translate_units(sentences, direction))
        messages = [
            {"role": "system", "content": (
                "You are a professional translator. %s "
                "Keep numbers, code, URLs and markup unchanged. Reply with the "
                "translation only, no explanations." % meta
            )},
            {"role": "user", "content": text},
        ]
        return self.chat(messages)
