using System.Text.Json;
using System.Text.Json.Nodes;
using Echo.Data;

namespace Echo.Learning;

/// <summary>一条对话消息；<see cref="Glosses"/> 是 AI 回合的逐词释义（原样 JSON 数组）。</summary>
internal sealed record ChatTurn(string Role, string Content, JsonArray? Glosses = null)
{
    public JsonObject ToJson()
    {
        var o = new JsonObject { ["role"] = Role, ["content"] = Content };
        if (Glosses is not null)
            o["glosses"] = Glosses.DeepClone();
        return o;
    }

    public static ChatTurn FromJson(JsonNode? node)
    {
        if (node is not JsonObject o)
            throw new FormatException("消息必须是对象");
        return new ChatTurn(o["role"]?.GetValue<string>() ?? throw new FormatException("消息缺 role"),
            o["content"]?.GetValue<string>() ?? throw new FormatException("消息缺 content"),
            o["glosses"] as JsonArray);
    }
}

/// <summary>用户数据里的对话历史、导入的单词表与设置（和学会的词同在 user.db）。</summary>
internal sealed class Store
{
    private readonly SqliteConnection _con;

    public Store(SqliteConnection connection)
    {
        _con = connection;
        lock (_con.Gate)
        {
            _con.Script("""
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
                """);
            var cols = _con.Query("PRAGMA table_info(conversations)").Select(r => (string)r[1]!).ToHashSet();
            if (!cols.Contains("practice"))
                _con.Execute("ALTER TABLE conversations ADD COLUMN practice TEXT NOT NULL DEFAULT ''");
            var messageCols = _con.Query("PRAGMA table_info(messages)").Select(r => (string)r[1]!).ToHashSet();
            if (!messageCols.Contains("glosses"))
                _con.Execute("ALTER TABLE messages ADD COLUMN glosses TEXT");
            _con.Script("PRAGMA foreign_keys = ON;");
        }
    }

    private static double Now() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    public string Setting(string key, string fallback)
    {
        lock (_con.Gate)
            return _con.QueryOne("SELECT value FROM settings WHERE key = ?", key)?[0] as string ?? fallback;
    }

    public void SetSetting(string key, string value)
    {
        lock (_con.Gate)
            _con.Execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", key, value);
    }

    /// <summary>整段替换一段对话的消息；id 为空就新建。新建时给了 title 就用它、不再自动命名（练词对话）。</summary>
    public long Save(long? id, IReadOnlyList<ChatTurn> messages, string level, JsonObject? practice = null, string? title = null)
    {
        var now = Now();
        lock (_con.Gate)
        {
            _con.Script("BEGIN;");
            try
            {
                long convId;
                if (id is null || _con.QueryOne("SELECT 1 FROM conversations WHERE id = ?", id.Value) is null)
                {
                    var first = messages.FirstOrDefault(m => m.Role == "user")?.Content ?? "";
                    var name = title ?? Clip(first, 40);
                    _con.Execute("INSERT INTO conversations (title, titled, level, created, updated, practice) VALUES (?, ?, ?, ?, ?, ?)",
                        string.IsNullOrEmpty(name) ? "新对话" : name, title is not null ? 1 : 0, level, now, now,
                        practice is null ? "" : practice.ToJsonString(Json.Options));
                    convId = _con.LastInsertRowId;
                }
                else
                {
                    convId = id.Value;
                    _con.Execute("UPDATE conversations SET level = ?, updated = ? WHERE id = ?", level, now, convId);
                }

                _con.Execute("DELETE FROM messages WHERE conv_id = ?", convId);
                for (var i = 0; i < messages.Count; i++)
                {
                    var m = messages[i];
                    _con.Execute("INSERT INTO messages (conv_id, seq, role, content, glosses) VALUES (?, ?, ?, ?, ?)",
                        convId, i, m.Role, m.Content, m.Glosses?.ToJsonString(Json.Options));
                }

                _con.Script("COMMIT;");
                return convId;
            }
            catch
            {
                _con.Script("ROLLBACK;");
                throw;
            }
        }
    }

    public JsonObject? Get(long id)
    {
        lock (_con.Gate)
        {
            var row = _con.QueryOne("SELECT id, title, titled, level, updated, practice FROM conversations WHERE id = ?", id);
            if (row is null)
                return null;
            var messages = new JsonArray();
            foreach (var m in _con.Query("SELECT role, content, glosses FROM messages WHERE conv_id = ? ORDER BY seq", id))
            {
                var o = new JsonObject { ["role"] = (string)m[0]!, ["content"] = (string)m[1]! };
                if (m[2] is string g)
                    o["glosses"] = JsonNode.Parse(g);
                messages.Add(o);
            }

            return new JsonObject
            {
                ["id"] = (long)row[0]!, ["title"] = (string)row[1]!, ["titled"] = (long)row[2]! != 0,
                ["level"] = (string)row[3]!, ["updated"] = Convert.ToDouble(row[4]),
                ["practice"] = row[5] is string { Length: > 0 } p ? JsonNode.Parse(p) : null,
                ["messages"] = messages,
            };
        }
    }

    /// <summary>按最近更新排；q 非空时在标题和全部消息里找，带上命中的那句话。</summary>
    public JsonArray List(string q, int limit = 200)
    {
        q = q.Trim();
        var result = new JsonArray();
        lock (_con.Gate)
        {
            if (q.Length == 0)
            {
                foreach (var r in _con.Query("SELECT id, title, updated FROM conversations ORDER BY updated DESC LIMIT ?", limit))
                    result.Add(new JsonObject { ["id"] = (long)r[0]!, ["title"] = (string)r[1]!, ["updated"] = Convert.ToDouble(r[2]), ["snippet"] = "" });
                return result;
            }

            var like = "%" + q.Replace("\\", "\\\\").Replace("%", "\\%").Replace("_", "\\_") + "%";
            var rows = _con.Query(
                "SELECT c.id, c.title, c.updated,"
                + " (SELECT content FROM messages m WHERE m.conv_id = c.id AND m.content LIKE ?1 ESCAPE '\\'"
                + "  ORDER BY seq LIMIT 1)"
                + " FROM conversations c"
                + " WHERE c.title LIKE ?1 ESCAPE '\\'"
                + "  OR EXISTS (SELECT 1 FROM messages m WHERE m.conv_id = c.id AND m.content LIKE ?1 ESCAPE '\\')"
                + " ORDER BY c.updated DESC LIMIT ?2", like, limit);
            foreach (var r in rows)
            {
                result.Add(new JsonObject
                {
                    ["id"] = (long)r[0]!, ["title"] = (string)r[1]!, ["updated"] = Convert.ToDouble(r[2]),
                    ["snippet"] = Around(r[3] as string ?? "", q),
                });
            }
        }

        return result;
    }

    public void SetTitle(long id, string title)
    {
        lock (_con.Gate)
            _con.Execute("UPDATE conversations SET title = ?, titled = 1 WHERE id = ?", title, id);
    }

    public void Delete(long id)
    {
        lock (_con.Gate)
        {
            _con.Execute("DELETE FROM messages WHERE conv_id = ?", id);
            _con.Execute("DELETE FROM conversations WHERE id = ?", id);
        }
    }

    public long AddWordList(string name, IReadOnlyList<WordItem> items)
    {
        lock (_con.Gate)
        {
            _con.Script("BEGIN;");
            try
            {
                _con.Execute("INSERT INTO wordlists (name, created) VALUES (?, ?)", name, Now());
                var id = _con.LastInsertRowId;
                for (var i = 0; i < items.Count; i++)
                    _con.Execute("INSERT INTO wordlist_words (list_id, seq, word, meaning, grp) VALUES (?, ?, ?, ?, ?)",
                        id, i, items[i].Word, items[i].Meaning, items[i].Group);
                _con.Script("COMMIT;");
                return id;
            }
            catch
            {
                _con.Script("ROLLBACK;");
                throw;
            }
        }
    }

    public JsonArray WordLists()
    {
        var result = new JsonArray();
        lock (_con.Gate)
        {
            foreach (var r in _con.Query("SELECT l.id, l.name, COUNT(w.seq), SUM(w.used > 0) FROM wordlists l"
                                         + " LEFT JOIN wordlist_words w ON w.list_id = l.id GROUP BY l.id ORDER BY l.created DESC"))
                result.Add(new JsonObject { ["id"] = (long)r[0]!, ["name"] = (string)r[1]!, ["count"] = (long)r[2]!, ["practiced"] = r[3] as long? ?? 0 });
        }

        return result;
    }

    public JsonObject? WordList(long id)
    {
        lock (_con.Gate)
        {
            var row = _con.QueryOne("SELECT id, name FROM wordlists WHERE id = ?", id);
            if (row is null)
                return null;
            var words = new JsonArray();
            foreach (var r in _con.Query("SELECT seq, word, meaning, grp, used FROM wordlist_words WHERE list_id = ? ORDER BY seq", id))
                words.Add(new JsonObject { ["seq"] = (long)r[0]!, ["word"] = (string)r[1]!, ["meaning"] = (string)r[2]!, ["group"] = (string)r[3]!, ["used"] = (long)r[4]! });
            return new JsonObject { ["id"] = (long)row[0]!, ["name"] = (string)row[1]!, ["words"] = words };
        }
    }

    public void DeleteWordList(long id)
    {
        lock (_con.Gate)
        {
            _con.Execute("DELETE FROM wordlist_words WHERE list_id = ?", id);
            _con.Execute("DELETE FROM wordlists WHERE id = ?", id);
        }
    }

    /// <summary>学习者在对话里亲口用上了这些词：各记一次。</summary>
    public void CountUsed(long listId, IEnumerable<string> words)
    {
        lock (_con.Gate)
        {
            foreach (var w in words)
                _con.Execute("UPDATE wordlist_words SET used = used + 1 WHERE list_id = ? AND word = ?", listId, w);
        }
    }

    internal static string Clip(string text, int n)
    {
        text = string.Join(' ', text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
        return text.Length <= n ? text : text[..(n - 1)] + "…";
    }

    /// <summary>命中词前后各留一点，列表里看得出是哪句话命中的。</summary>
    internal static string Around(string text, string q, int width = 36)
    {
        text = string.Join(' ', text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
        var i = text.IndexOf(q, StringComparison.OrdinalIgnoreCase);
        if (i < 0)
            return "";
        var start = Math.Max(0, i - width / 3);
        var snippet = text.Substring(start, Math.Min(width, text.Length - start));
        return (start > 0 ? "…" : "") + snippet + (start + width < text.Length ? "…" : "");
    }
}

/// <summary>JSON 序列化共用选项：中文不转义。</summary>
internal static class Json
{
    public static readonly JsonSerializerOptions Options = new()
    {
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    public static string Serialize(JsonNode? node) => node?.ToJsonString(Options) ?? "null";
}
