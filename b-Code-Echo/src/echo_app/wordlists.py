"""从 Word（.docx）单词表里取词。

不装 python-docx：.docx 是个 zip，正文在 word/document.xml，按顺序读段落和表格就够了。
支持两种排法（本机 2026-008 的单词表就是这两种）：
- 每段一个词（或词组），可在后面跟释义：「spacious 宽敞的」。
- 表格一行一个词，单元格依次是词、音标、释义；表头行（单词 Word…）自动跳过。
不是词的段落（「L1 1-1」「一、灾难 Disaster」）当作分组标题，后面的词归到这一组。
"""

import io
import re
import zipfile
from xml.etree import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# 词或词组：英文字母开头，只含字母、空格、连字符、撇号，最多 4 个词
_ENTRY = re.compile(r"^([A-Za-z][A-Za-z'’\-]*(?: [A-Za-z][A-Za-z'’\-]*){0,3})(?=$|[\s/\[(（，,;；:：.]|[一-鿿])")
_CJK = re.compile(r"[一-鿿]")
_PHONETIC = re.compile(r"^(/[^/]*/|\[[^\]]*\])")


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter(_W + "t")).strip()


def _entry(text: str) -> tuple[str, str] | None:
    """一段文字是不是一个词条：是就返回 (词, 释义)；释义可能为空。"""
    text = " ".join(text.split())
    m = _ENTRY.match(text)
    if not m:
        return None
    rest = _PHONETIC.sub("", text[m.end():].strip(), count=1).strip()
    if rest and not _CJK.search(rest):
        return None  # 后面跟的是英文：是句子或标题（Terrorism & Conflict），不是词条
    return m.group(1).replace("’", "'"), rest


def parse_docx(data: bytes) -> list[dict]:
    """返回 [{word, meaning, group}]，按文档顺序，同一个词只留第一次。"""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(_W + "body")
    group, seen, out = "", set(), []

    def add(word: str, meaning: str) -> None:
        if word.lower() not in seen:
            seen.add(word.lower())
            out.append({"word": word, "meaning": meaning, "group": group})

    for el in body if body is not None else []:
        if el.tag == _W + "p":
            text = _text(el)
            if not text:
                continue
            hit = _entry(text)
            if hit:
                add(*hit)
            elif len(text) <= 40:
                group = text
        elif el.tag == _W + "tbl":
            for tr in el.iter(_W + "tr"):
                cells = [_text(tc) for tc in tr.iter(_W + "tc")]
                hit = next(filter(None, map(_entry, cells)), None)
                if not hit:
                    continue  # 表头（单词 Word）、空行
                word, meaning = hit
                if not meaning:  # 释义在别的单元格：取第一个带中文的
                    meaning = next((c for c in cells if _CJK.search(c)), "")
                add(word, " ".join(meaning.split()))
    return out
