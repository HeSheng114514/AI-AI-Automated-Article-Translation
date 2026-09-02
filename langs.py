# -*- coding: utf-8 -*-
"""
语言支持模块
============
- 词典语言推断（依据文件名中的关键词：英/日/韩/法/德/俄…）
- 文本语言检测（按文字体系：假名/谚文/西里尔/阿拉伯/泰文/汉字/拉丁…）
- 方向码（"en2zh" / "ja2zh"…）与界面标签、接口提示语的转换
- Dictionary 文件夹扫描
"""
import os
import re

# ------------------------- 语言元信息 -------------------------
# code -> (界面单字, 接口目标语言提示词)
LANG_CN = {
    "en": "英", "zh": "中", "ja": "日", "ko": "韩",
    "fr": "法", "de": "德", "ru": "俄", "es": "西",
    "it": "意", "pt": "葡", "th": "泰", "vi": "越",
    "ar": "阿", "hi": "印", "el": "希", "nl": "荷",
    "pl": "波", "tr": "土",
}
# 供 AI 接口提示使用的目标语言名
TARGET_NAMES = {
    "zh": "简体中文", "en": "English", "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "ru": "俄语", "es": "西班牙语",
    "it": "意大利语", "pt": "葡萄牙语", "th": "泰语", "vi": "越南语",
    "ar": "阿拉伯语", "hi": "印地语", "el": "希腊语", "nl": "荷兰语",
    "pl": "波兰语", "tr": "土耳其语",
}

# 使用空格分词的语言（按“词”查词典）
TOKEN_LANGS = {"en", "fr", "de", "es", "it", "pt", "vi", "nl", "pl",
               "tr", "ru", "el", "ar", "hi", "he"}
# 同属拉丁字母体系（检测结果统一为 'latin'，词典之间无法按字形区分）
LATIN_FAMILY = {"en", "fr", "de", "es", "it", "pt", "vi", "nl", "pl", "tr"}
# 不使用空格分词的语言：按词典词条对原文做“最长匹配”
LONGEST_MATCH_LANGS = {"ja", "ko", "th", "zh"}

# 词典文件名的识别关键词（长词条在前，避免 "ja" 误中 "japanese" 之类）
_FILENAME_HINTS = [
    ("日本語", "ja"), ("japanese", "ja"), ("日语", "ja"), ("日文", "ja"),
    ("한국어", "ko"), ("korean", "ko"), ("韩语", "ko"), ("韩文", "ko"),
    ("english", "en"), ("英语", "en"), ("英文", "en"),
    ("russian", "ru"), ("俄语", "ru"), ("俄文", "ru"),
    ("french", "fr"), ("法语", "fr"), ("法文", "fr"),
    ("german", "de"), ("德语", "de"), ("德文", "de"),
    ("spanish", "es"), ("西班牙语", "es"), ("西语", "es"),
    ("italian", "it"), ("意大利语", "it"),
    ("portuguese", "pt"), ("葡萄牙语", "pt"),
    ("thai", "th"), ("泰语", "th"), ("泰文", "th"),
    ("vietnamese", "vi"), ("viet", "vi"), ("越南语", "vi"),
    ("arabic", "ar"), ("阿拉伯语", "ar"),
    ("hindi", "hi"), ("印地语", "hi"),
    ("greek", "el"), ("希腊语", "el"),
    # 常见“X 汉”字样兜底
    ("日汉", "ja"), ("汉日", "ja"),
    ("韩汉", "ko"), ("汉韩", "ko"),
    ("英汉", "en"), ("汉英", "en"),
    ("法汉", "fr"), ("德汉", "de"), ("俄汉", "ru"),
    ("西汉", "es"), ("意汉", "it"), ("葡汉", "pt"),
]

_DICT_EXTS = (".txt", ".dic", ".dict")


def lang_label(code):
    return LANG_CN.get(code, code)


def filename_language(filename):
    """根据文件名推断词典语言代码，推断不出默认 'en'。"""
    name = os.path.basename(filename).lower()
    for kw, code in _FILENAME_HINTS:
        if kw.lower() in name:
            return code
    return "en"


def default_dictionary_folder():
    """Dictionary 文件夹 = 程序目录 / Dictionary。"""
    import config
    return os.path.join(config.APP_DIR, "Dictionary")


def list_dictionaries(folder=None):
    """扫描 Dictionary 文件夹，返回 [(文件名, 语言码), ...] 按文件名排序。"""
    folder = folder or default_dictionary_folder()
    result = []
    if os.path.isdir(folder):
        for fn in sorted(os.listdir(folder)):
            if fn.lower().endswith(_DICT_EXTS) and os.path.isfile(os.path.join(folder, fn)):
                result.append((fn, filename_language(fn)))
    return result


def describe_dictionary(fn):
    """词典描述：英汉词典.txt（英语 → 中文）"""
    code = filename_language(fn)
    if code in ("en", "zh"):
        other = "英" if code == "en" else "汉"
        return "%s（%s语 → 中文）" % (fn, other)
    return "%s（%s语 → 中文）" % (fn, lang_label(code))


# ------------------------- 文本语言检测 -------------------------
_HIRA = 0x3040
_KATA = 0x30A0
_HANGUL_A = 0xAC00
_HANGUL_B = 0xD7A3
_HANGUL_J = 0x1100  # 谚文字母
_HANGUL_K = 0x11FF
_CJK_A = 0x4E00
_CJK_B = 0x9FFF
_CYR_A = 0x0400
_CYR_B = 0x04FF
_AR_A = 0x0600
_AR_B = 0x06FF
_TH_A = 0x0E00
_TH_B = 0x0E7F
_DEVA_A = 0x0900
_DEVA_B = 0x097F
_HEB_A = 0x0590
_HEB_B = 0x05FF
_GRK_A = 0x0370
_GRK_B = 0x03FF


def script_counts(text):
    """统计各文字体系字符数，返回 dict。"""
    latin = 0
    hira = kata = hangul = cjk = cyr = arab = thai = deva = hebr = greek = 0
    other_alpha = 0
    for ch in text:
        o = ord(ch)
        if 0x41 <= o <= 0x5A or 0x61 <= o <= 0x7A:
            latin += 1
        elif _HIRA <= o < _KATA:
            hira += 1
        elif _KATA <= o <= 0x30FF:
            kata += 1
        elif _HANGUL_A <= o <= _HANGUL_B or _HANGUL_J <= o <= _HANGUL_K:
            hangul += 1
        elif _CJK_A <= o <= _CJK_B:
            cjk += 1
        elif _CYR_A <= o <= _CYR_B:
            cyr += 1
        elif _AR_A <= o <= _AR_B:
            arab += 1
        elif _TH_A <= o <= _TH_B:
            thai += 1
        elif _DEVA_A <= o <= _DEVA_B:
            deva += 1
        elif _HEB_A <= o <= _HEB_B:
            hebr += 1
        elif _GRK_A <= o <= _GRK_B:
            greek += 1
        elif ch.isalpha():
            other_alpha += 1
    return {"latin": latin, "hira": hira, "kata": kata, "hangul": hangul,
            "cjk": cjk, "cyr": cyr, "arab": arab, "thai": thai,
            "deva": deva, "hebr": hebr, "greek": greek,
            "other": other_alpha}


def detect_language(text):
    """
    按文字体系判断文本语言代码：
    ja/ko/ru/ar/th/hi/he/el/zh/latin，无法判断返回 None。
    返回 'latin' 表示拉丁字母文本（无法细分英/法/德…）。
    """
    if not text or not text.strip():
        return None
    c = script_counts(text)
    if c["hira"] or c["kata"]:
        return "ja"
    if c["hangul"]:
        return "ko"
    if c["cyr"]:
        return "ru"
    if c["arab"]:
        return "ar"
    if c["thai"]:
        return "th"
    if c["deva"]:
        return "hi"
    if c["hebr"]:
        return "he"
    if c["greek"]:
        return "el"
    if c["cjk"]:
        return "zh"
    if c["latin"] or c["other"]:
        return "latin"
    return None


def lang_matches_detected(code, detected):
    """词典/目标语言码 code 是否匹配检测结果 detected。"""
    if code in LATIN_FAMILY:
        return detected == "latin"
    return code == detected


# ------------------------- 方向（AI 模式用） -------------------------
def lang_name(code):
    return LANG_CN.get(code, code)


def target_language_name(code):
    """给 AI 接口的“目标语言”提示词。"""
    return TARGET_NAMES.get(code, "English" if code == "en" else code)


def direction_pair(direction):
    """'ja2zh' -> ('ja','zh')；非法时回退 ('en','zh')。"""
    if isinstance(direction, str) and "2" in direction:
        src, dst = direction.split("2", 1)
        if src and dst:
            return src, dst
    return "en", "zh"


def pair_str(src, dst):
    return "%s2%s" % (src, dst)


def direction_label(src, dst):
    """('ja','zh') -> '日 → 中'"""
    return "%s → %s" % (lang_label(src), lang_label(dst))


# ------------------------- 双下拉语言选择（源语言 → 目标语言） -------------------------
# 完整中文名显示（下拉里更易读）
DISPLAY_NAMES = {
    "en": "英语", "zh": "中文", "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "ru": "俄语", "es": "西班牙语",
    "it": "意大利语", "pt": "葡萄牙语", "th": "泰语", "vi": "越南语",
    "ar": "阿拉伯语", "hi": "印地语", "el": "希腊语", "nl": "荷兰语",
    "pl": "波兰语", "tr": "土耳其语",
}
# 下拉中语言的排列顺序
UI_LANG_ORDER = ["en", "zh", "ja", "ko", "fr", "de", "ru", "es",
                 "it", "pt", "th", "vi", "ar", "hi", "el", "nl", "pl", "tr"]
_CODE_BY_DISPLAY = {name: code for code, name in DISPLAY_NAMES.items()}
AUTO_NAME = "自动识别"


def source_language_options():
    """源语言下拉：自动识别 + 全部语言。"""
    return [AUTO_NAME] + [DISPLAY_NAMES[c] for c in UI_LANG_ORDER]


def target_language_options():
    """目标语言下拉：全部语言。"""
    return [DISPLAY_NAMES[c] for c in UI_LANG_ORDER]


def display_name(code):
    """语言码 -> 下拉显示名（auto -> '自动识别'）。"""
    if code == "auto":
        return AUTO_NAME
    return DISPLAY_NAMES.get(code, code)


def display_code(name):
    """下拉显示名 -> 语言码（'自动识别' -> 'auto'）。"""
    name = (name or "").strip()
    if name == AUTO_NAME:
        return "auto"
    return _CODE_BY_DISPLAY.get(name, "en")


def pair_display(src, dst):
    """('ja','vi') -> '日语 → 越南语'（用于界面显示/日志）。"""
    return "%s → %s" % (display_name(src), display_name(dst))
