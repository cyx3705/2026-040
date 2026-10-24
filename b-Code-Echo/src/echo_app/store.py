"""用户数据里的对话历史、导入的单词表与设置（和学会的词同在 user.db）。"""

import json
import sqlite3
import threading
import time

from . import config


class Store:
    def __init__(self) -> None:
        config.USER_DB.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(config.USER_DB, check_same_thread=False)
        self._lock = threading.RLock()  # 读也要锁：一个连接被线程池里几个请求同时用会出错
        self._con.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, titled INTEGER NOT NULL DEFAULT 0,
                level TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                conv_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                PRIMARY KEY (conv_id, seq));
            CREATE TABLE IF NOT EXISTS wordlists (id INTEGER PRIMARY KEY, name TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS wordlist_words (
                list_id INTEGER NOT NULL, seq INTEGER NOT NULL, word TEXT NOT NULL, meaning TEXT NOT NULL,
                grp TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (list_id, seq));
            """
        )
        # 练词对话要记住练的是哪些词（JSON）；老库没有这一列就补上
        cols = {r[1] for r in self._con.execute("PRAGMA table_info(conversations)")}
        if "practice" not in cols:
            self._con.execute("ALTER TABLE conversations ADD COLUMN practice TEXT NOT NULL DEFAULT ''")
        message_cols = {r[1] for r in self._con.execute("PRAGMA table_info(messages)")}
        if "glosses" not in message_cols:
            self._con.execute("ALTER TABLE messages ADD COLUMN glosses TEXT")
        self._con.execute("PRAGMA foreign_keys = ON")
        self._con.commit()

    def _all(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._con.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> tuple | None:
        with self._lock:
            return self._con.execute(sql, params).fetchone()

    def setting(self, key: str, default: str) -> str:
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._con.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
            self._con.commit()

    def save(
        self, conv_id: int | None, messages: list[dict], level: str,
        practice: dict | None = None, title: str | None = None,
    ) -> int:
        """整段替换一段对话的消息；conv_id 为空就新建。

        新建时给了 title 就用它、不再自动命名（练词对话）；否则先用第一句话，等自动命名。
        """
        now = time.time()
        with self._lock:
            if conv_id is None or not self._exists(conv_id):
                first = next((m["content"] for m in messages if m["role"] == "user"), "")
                cur = self._con.execute(
                    "INSERT INTO conversations (title, titled, level, created, updated, practice)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (title or _clip(first, 40) or "新对话", int(bool(title)), level, now, now,
                     json.dumps(practice, ensure_ascii=False) if practice else ""),
                )
                conv_id = cur.lastrowid
            else:
                self._con.execute(
                    "UPDATE conversations SET level = ?, updated = ? WHERE id = ?", (level, now, conv_id)
                )
            self._con.execute("DELETE FROM messages WHERE conv_id = ?", (conv_id,))
            self._con.executemany(
                "INSERT INTO messages (conv_id, seq, role, content, glosses) VALUES (?, ?, ?, ?, ?)",
                [(conv_id, i, m["role"], m["content"],
                  json.dumps(m["glosses"], ensure_ascii=False) if m.get("glosses") is not None else None)
                 for i, m in enumerate(messages)],
            )
            self._con.commit()
        return conv_id

    def _exists(self, conv_id: int) -> bool:
        return self._one("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)) is not None

    def get(self, conv_id: int) -> dict | None:
        row = self._one(
            "SELECT id, title, titled, level, updated, practice FROM conversations WHERE id = ?", (conv_id,)
        )
        if not row:
            return None
        msgs = self._all(
            "SELECT role, content, glosses FROM messages WHERE conv_id = ? ORDER BY seq", (conv_id,)
        )
        return {
            "id": row[0], "title": row[1], "titled": bool(row[2]), "level": row[3], "updated": row[4],
            "practice": json.loads(row[5]) if row[5] else None,
            "messages": [{"role": r, "content": c,
                          **({"glosses": json.loads(g)} if g is not None else {})} for r, c, g in msgs],
        }

    def list(self, q: str = "", limit: int = 200) -> list[dict]:
        """按最近更新排；q 非空时在标题和全部消息里找，带上命中的那句话。"""
        q = q.strip()
        if not q:
            rows = self._all(
                "SELECT id, title, updated FROM conversations ORDER BY updated DESC LIMIT ?", (limit,)
            )
            return [{"id": i, "title": t, "updated": u, "snippet": ""} for i, t, u in rows]
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self._all(
            "SELECT c.id, c.title, c.updated,"
            " (SELECT content FROM messages m WHERE m.conv_id = c.id AND m.content LIKE ?1 ESCAPE '\\'"
            "  ORDER BY seq LIMIT 1)"
            " FROM conversations c"
            " WHERE c.title LIKE ?1 ESCAPE '\\'"
            "  OR EXISTS (SELECT 1 FROM messages m WHERE m.conv_id = c.id AND m.content LIKE ?1 ESCAPE '\\')"
            " ORDER BY c.updated DESC LIMIT ?2",
            (like, limit),
        )
        return [{"id": i, "title": t, "updated": u, "snippet": _around(s or "", q)} for i, t, u, s in rows]

    def set_title(self, conv_id: int, title: str) -> None:
        with self._lock:
            self._con.execute("UPDATE conversations SET title = ?, titled = 1 WHERE id = ?", (title, conv_id))
            self._con.commit()

    def delete(self, conv_id: int) -> None:
        with self._lock:
            self._con.execute("DELETE FROM messages WHERE conv_id = ?", (conv_id,))
            self._con.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            self._con.commit()

    # ---- 导入的单词表 ----

    def add_wordlist(self, name: str, items: list[dict]) -> int:
        with self._lock:
            cur = self._con.execute("INSERT INTO wordlists (name, created) VALUES (?, ?)", (name, time.time()))
            list_id = cur.lastrowid
            self._con.executemany(
                "INSERT INTO wordlist_words (list_id, seq, word, meaning, grp) VALUES (?, ?, ?, ?, ?)",
                [(list_id, i, it["word"], it["meaning"], it["group"]) for i, it in enumerate(items)],
            )
            self._con.commit()
        return list_id

    def wordlists(self) -> list[dict]:
        rows = self._all(
            "SELECT l.id, l.name, COUNT(w.seq), SUM(w.used > 0) FROM wordlists l"
            " LEFT JOIN wordlist_words w ON w.list_id = l.id GROUP BY l.id ORDER BY l.created DESC"
        )
        return [{"id": i, "name": n, "count": c, "practiced": p or 0} for i, n, c, p in rows]

    def wordlist(self, list_id: int) -> dict | None:
        row = self._one("SELECT id, name FROM wordlists WHERE id = ?", (list_id,))
        if not row:
            return None
        words = self._all(
            "SELECT seq, word, meaning, grp, used FROM wordlist_words WHERE list_id = ? ORDER BY seq", (list_id,)
        )
        return {
            "id": row[0], "name": row[1],
            "words": [{"seq": s, "word": w, "meaning": m, "group": g, "used": u} for s, w, m, g, u in words],
        }

    def delete_wordlist(self, list_id: int) -> None:
        with self._lock:
            self._con.execute("DELETE FROM wordlist_words WHERE list_id = ?", (list_id,))
            self._con.execute("DELETE FROM wordlists WHERE id = ?", (list_id,))
            self._con.commit()

    def count_used(self, list_id: int, words: list[str]) -> None:
        """学习者在对话里亲口用上了这些词：各记一次。"""
        if not words:
            return
        with self._lock:
            self._con.executemany(
                "UPDATE wordlist_words SET used = used + 1 WHERE list_id = ? AND word = ?",
                [(list_id, w) for w in words],
            )
            self._con.commit()


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _around(text: str, q: str, width: int = 36) -> str:
    """命中词前后各留一点，列表里看得出是哪句话命中的。"""
    text = " ".join(text.split())
    i = text.lower().find(q.lower())
    if i < 0:
        return ""
    start = max(0, i - width // 3)
    snippet = text[start : start + width]
    return ("…" if start else "") + snippet + ("…" if start + width < len(text) else "")
