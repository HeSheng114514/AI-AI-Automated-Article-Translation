# -*- coding: utf-8 -*-
"""
配置模块：读写 config.json（与程序同目录），保存 AI 接口与各类选项。
"""
import json
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

_DEFAULTS = {
    # ---- AI 接口（OpenAI 兼容）----
    "base_url": "https://api.deepseek.com/v1",   # 例如 DeepSeek / OpenAI / 通义 / 智谱
    "api_key": "",
    "model": "deepseek-chat",                    # 例如 deepseek-chat / gpt-4o-mini
    "ai_timeout": 90,
    # ---- AI 方向 ----
    "direction": "en2zh",      # 语言对：en2zh / zh2en / ja2zh / auto2zh…
    # ---- 输出 ----
    "out_encoding": "utf-8",   # utf-8 / utf-8-sig / gb18030
    # ---- 选项 ----
    "json_translate_keys": False,   # JSON 是否连键名一起翻译
    "skip_code_like": True,         # 跳过疑似 代码/路径/URL/数字 的文本
    "csv_skip_header": True,        # CSV 首行视为表头不翻译
    "recursive": True,              # 添加文件夹时递归子目录
}

_ENC_LABELS = {
    "utf-8": "UTF-8（无 BOM）",
    "utf-8-sig": "UTF-8（含 BOM）",
    "gb18030": "GBK / GB18030（中文）",
}


class Settings:
    """轻量设置对象，属性式访问。"""

    def __init__(self, values=None):
        self._d = dict(_DEFAULTS)
        if values:
            for k, v in values.items():
                if k in self._d:
                    self._d[k] = v

    def __getattr__(self, name):
        if name in self._d:
            return self._d[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        elif name in self._d:
            self._d[name] = value
        else:
            object.__setattr__(self, name, value)

    def as_dict(self):
        return dict(self._d)

    # ------------------------------------------------------------------
    @property
    def api_configured(self):
        """三个字段都填了才算配置完整（未验证网络）。"""
        return bool(self.base_url and self.api_key and self.model)

    @property
    def out_encoding_label(self):
        return _ENC_LABELS.get(self.out_encoding, self.out_encoding)

    # ------------------------------------------------------------------
    def save(self, path=CONFIG_PATH):
        data = dict(self._d)
        # API Key 不做明文混淆保护；如需更安全可自行替换为系统凭据管理器。
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path=CONFIG_PATH):
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                return cls(saved)
            except Exception:
                return cls()
        return cls()
