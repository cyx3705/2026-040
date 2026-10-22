"""查词与「学会的词」：ECDICT 离线词典 + 用户自己的学会记录。"""

import csv
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import config
from .glosses import CONTRACTION_TAILS, GLOSSES, NOT_BASES

# 释义里的领域标注、括号补充、词性前缀，做短释义时都去掉。
_DOMAIN = re.compile(r"^\[[^\]]*\]\s*")
_POS = re.compile(r"^((?:[a-z]+\.\s*)+)")
_PAREN = re.compile(r"[（(][^）)]*[）)]")
_SPLIT = re.compile(r"[,，;；]")
# 「be的现在时复数」这类释义指向另一个词
_REFERS = re.compile(r"^([a-z]+)的")

VERB = ("v.", "vt.", "vi.")
NOUN = ("n.",)


def _senses(translation: str) -> list[tuple[str, str]]:
    """拆成 (词性, 第一个义项)，跳过领域术语行和「字母」行。"""
    result = []
    for line in translation.split("\n"):
        line = line.strip()
        if not line or line.startswith("[") or "字母" in line:
            continue
        line = _DOMAIN.sub("", line)
        m = _POS.match(line)
        pos = m.group(1).split()[0] if m else ""
        rest = _PAREN.sub("", line[m.end():] if m else line)
        first = next((p.strip() for p in _SPLIT.split(rest) if p.strip()), "")
        if first:
            result.append((pos, first))
    return result


def short_gloss(translation: str, prefer: tuple[str, ...] = ()) -> str:
    """取一个短释义作为词下方的小字。

    有偏好词性时先找那个词性；没有偏好时，首行是名词而后面有很短的形容词义的，
    取形容词（sweet 名词义「糖果」排在前面，常用的却是「甜的」）。形容词义长的
    多是派生义（fun 的「供娱乐用的」），仍取名词义。
    """
    senses = _senses(translation)
    if not senses:
        return ""
    for pos, text in senses:
        if pos in prefer:
            return text
    if not prefer and senses[0][0] == "n.":
        for pos, text in senses:
            if pos == "a." and len(text) <= 3:
                return text
    return senses[0][1]


def build_db(csv_path: Path, db_path: Path) -> int:
    """把 ecdict.csv 转成 SQLite，只留查词要用的列。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute(
        "CREATE TABLE words (word TEXT PRIMARY KEY COLLATE NOCASE, phonetic TEXT,"
        " translation TEXT, exchange TEXT, frq INTEGER, tag TEXT)"
    )
    rows = 0
    with open(csv_path, encoding="utf-8", newline="") as f:
        batch = []
        for r in csv.DictReader(f):
            # ECDICT 里换行写成字面的 \n（个别是 \r\n）
            tr = (r["translation"] or "").replace("\\r", "").replace("\\n", "\n").strip()
            if not tr:
                continue
            frq = int(r["frq"]) if (r["frq"] or "").isdigit() else 0
            batch.append((r["word"], r["phonetic"], tr, r["exchange"], frq, r["tag"]))
            if len(batch) >= 20000:
                con.executemany("INSERT OR IGNORE INTO words VALUES (?,?,?,?,?,?)", batch)
                rows += len(batch)
                batch.clear()
        con.executemany("INSERT OR IGNORE INTO words VALUES (?,?,?,?,?,?)", batch)
        rows += len(batch)
    con.commit()
    con.close()
    tmp.replace(db_path)
    return rows


class Dictionary:
    def __init__(self) -> None:
        # 词典连接每个线程一个：接口在线程池里并发查词，共用一个连接会报 API misuse
        self._local = threading.local()
        config.USER_DB.parent.mkdir(parents=True, exist_ok=True)
        self._user = sqlite3.connect(config.USER_DB, check_same_thread=False)
        self._user_lock = threading.Lock()
        self._user.execute(
            "CREATE TABLE IF NOT EXISTS learned (lemma TEXT PRIMARY KEY, learned_at REAL)"
        )
        self._user.commit()

    @property
    def installed(self) -> bool:
        return config.DICT_DB.is_file()

    def _con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._local.con = sqlite3.connect(f"file:{config.DICT_DB}?mode=ro", uri=True)
        return con

    def _row(self, word: str) -> tuple | None:
        return self._con().execute(
            "SELECT word, phonetic, translation, exchange FROM words WHERE word = ?", (word,)
        ).fetchone()

    @staticmethod
    def _exchange(row: tuple) -> tuple[str | None, set[str]]:
        """exchange 形如 "0:go/1:p"：0 是原形，1 是本词相对原形的变形类别。"""
        lemma, kinds = None, set()
        for item in (row[3] or "").split("/"):
            if item.startswith("0:"):
                lemma = item[2:]
            elif item.startswith("1:"):
                kinds = set(item[2:])
        return lemma, kinds

    def _gloss_of(self, word: str, prefer: tuple[str, ...] = ()) -> str:
        if word in GLOSSES:
            return GLOSSES[word]
        row = self._row(word)
        return short_gloss(row[2], prefer) if row else ""

    def _entry(self, w: str) -> dict | None:
        row = self._row(w)
        if not row:
            return {"lemma": w, "phonetic": "", "gloss": GLOSSES[w], "translation": ""} if w in GLOSSES else None
        key, tr = w, row[2]
        lemma, kinds = self._exchange(row)
        base = self._row(lemma) if lemma else None
        if base and kinds & set("pdi3"):  # 动词变形：went → go，按动词义取
            key, tr = lemma, base[2]
            gloss = self._gloss_of(lemma, VERB)
        elif base and "s" in kinds:  # 名词复数：按名词义取
            key, tr = lemma, base[2]
            gloss = self._gloss_of(lemma, NOUN)
        else:  # 原形；比较级、最高级（better）用自己的词条
            gloss = GLOSSES.get(w) or short_gloss(tr)
        if (m := _REFERS.match(gloss)) and m.group(1) != w:  # am：「be的单数第一人称」
            key = m.group(1)
            gloss = self._gloss_of(key, VERB)
        if not gloss:
            return None
        return {"lemma": key, "phonetic": row[1] or "", "gloss": gloss, "translation": tr}

    def lookup(self, word: str) -> dict | None:
        """查一个词。变形词归到原形，学会状态记在 lemma 上。"""
        w = word.lower().replace("’", "'")
        if w.endswith("n't"):  # don't → 不做，can't → 不能
            base = w[:-3]
            g = self._gloss_of(NOT_BASES.get(base, base), VERB)
            return {"lemma": w, "phonetic": "", "gloss": "不" + g, "translation": ""} if g else None
        for tail, tail_gloss in CONTRACTION_TAILS.items():
            if w.endswith(tail) and len(w) > len(tail):  # that's → 那是
                g = self._gloss_of(w[: -len(tail)])
                return {"lemma": w, "phonetic": "", "gloss": g + tail_gloss, "translation": ""} if g else None
        return self._entry(w)

    def complete(self, prefix: str, n: int) -> list[str]:
        """以 prefix 开头的常用词，按词频（frq 是排名，越小越常用）。"""
        if not self.installed or not prefix.isalpha():
            return []
        rows = self._con().execute(
            "SELECT word FROM words WHERE word LIKE ? AND frq > 0 AND word NOT LIKE '% %'"
            " AND word NOT LIKE '%-%' ORDER BY frq LIMIT ?",
            (prefix + "%", n),
        ).fetchall()
        return [r[0] for r in rows]

    def lookup_many(self, words: list[str]) -> dict:
        if not self.installed:
            return {}
        return {w: self.lookup(w) for w in dict.fromkeys(words)}

    def learned(self) -> list[dict]:
        with self._user_lock:
            rows = self._user.execute(
                "SELECT lemma FROM learned ORDER BY learned_at DESC"
            ).fetchall()
        result = []
        for (lemma,) in rows:
            entry = self.lookup(lemma) if self.installed else None
            result.append({"lemma": lemma, "gloss": entry["gloss"] if entry else ""})
        return result

    def set_learned(self, lemma: str, learned: bool) -> None:
        with self._user_lock:
            if learned:
                self._user.execute(
                    "INSERT OR REPLACE INTO learned VALUES (?, ?)", (lemma, time.time())
                )
            else:
                self._user.execute("DELETE FROM learned WHERE lemma = ?", (lemma,))
            self._user.commit()
