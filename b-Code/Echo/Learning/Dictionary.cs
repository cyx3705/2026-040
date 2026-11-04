using System.Collections.Concurrent;
using System.Text;
using System.Text.RegularExpressions;
using Echo.Data;

namespace Echo.Learning;

/// <summary>一条查词结果。</summary>
internal sealed record DictEntry(string Lemma, string Phonetic, string Gloss, string Translation);

/// <summary>一个候选义项：词性 + 中文。</summary>
internal sealed record Sense(string Pos, string Text);

/// <summary>
/// 查词与「学会的词」：ECDICT 离线词典（只读）+ 用户库里的学会记录。
/// </summary>
internal sealed partial class Dictionary : IDisposable
{
    private static readonly string[] Verb = ["v.", "vt.", "vi."];
    private static readonly string[] Noun = ["n."];

    private readonly string _path;
    private readonly SqliteConnection _user;
    private readonly ConcurrentDictionary<string, IReadOnlyList<string>> _choices = new(StringComparer.Ordinal);
    private SqliteConnection? _dict;

    public Dictionary(string dictionaryPath, SqliteConnection user)
    {
        _path = dictionaryPath;
        _user = user;
        lock (_user.Gate)
            _user.Execute("CREATE TABLE IF NOT EXISTS learned (lemma TEXT PRIMARY KEY, learned_at REAL)");
    }

    public bool Installed => File.Exists(_path);

    public void Dispose() => _dict?.Dispose();

    [GeneratedRegex(@"^\[[^\]]*\]\s*")] private static partial Regex DomainRx();
    [GeneratedRegex(@"^((?:[a-z]+\.\s*)+)")] private static partial Regex PosRx();
    [GeneratedRegex(@"[（(][^）)]*[）)]")] private static partial Regex ParenRx();
    [GeneratedRegex(@"[,，;；]")] private static partial Regex SplitRx();
    [GeneratedRegex(@"^([a-z]+)的")] private static partial Regex RefersRx();
    [GeneratedRegex(@"^[A-Za-z]+(?:[' -][A-Za-z]+){0,3}$")] private static partial Regex ChoiceWordRx();

    private SqliteConnection Db
    {
        get
        {
            if (_dict is { } db)
                return db;
            lock (this)
                return _dict ??= new SqliteConnection("file:" + _path.Replace('\\', '/') + "?mode=ro", readOnly: true);
        }
    }

    private object?[]? Row(string word)
    {
        var db = Db;
        lock (db.Gate)
            return db.QueryOne("SELECT word, phonetic, translation, exchange FROM words WHERE word = ?", word);
    }

    /// <summary>拆成 (词性, 第一个义项)，跳过领域术语行和「字母」行。</summary>
    internal static List<(string Pos, string Text)> ShortSenses(string translation)
    {
        var result = new List<(string, string)>();
        foreach (var raw in translation.Split('\n'))
        {
            var line = raw.Trim();
            if (line.Length == 0 || line.StartsWith('[') || line.Contains("字母"))
                continue;
            line = DomainRx().Replace(line, "", 1);
            var m = PosRx().Match(line);
            var pos = m.Success ? m.Groups[1].Value.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries)[0] : "";
            var rest = ParenRx().Replace(m.Success ? line[m.Length..] : line, "");
            var first = SplitRx().Split(rest).Select(p => p.Trim()).FirstOrDefault(p => p.Length > 0) ?? "";
            if (first.Length > 0)
                result.Add((pos, first));
        }

        return result;
    }

    /// <summary>完整词条里的候选义项（同一词性行里的全部逗号义项），供上下文选义使用。</summary>
    internal static List<Sense> SenseCandidates(string translation)
    {
        var result = new List<Sense>();
        var seen = new HashSet<(string, string)>();
        foreach (var raw in translation.Split('\n'))
        {
            var line = raw.Trim();
            if (line.Length == 0 || line.Contains("字母"))
                continue;
            var dm = DomainRx().Match(line);
            var domain = dm.Success ? dm.Value.Trim() : "";
            line = DomainRx().Replace(line, "", 1);
            var m = PosRx().Match(line);
            var pos = m.Success ? m.Groups[1].Value.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries)[0] : "";
            var rest = m.Success ? line[m.Length..] : line;
            var parts = new List<string>();
            var buffer = new StringBuilder();
            var depth = 0;
            foreach (var c in rest)
            {
                if ("（([【".Contains(c))
                    depth++;
                else if ("）)]】".Contains(c))
                    depth = Math.Max(0, depth - 1);
                if (",，;；".Contains(c) && depth == 0)
                {
                    parts.Add(buffer.ToString());
                    buffer.Clear();
                }
                else
                {
                    buffer.Append(c);
                }
            }

            parts.Add(buffer.ToString());
            foreach (var part in parts)
            {
                var text = part.Trim();
                if (text.Length == 0)
                    continue;
                if (domain.Length > 0)
                    text = domain + " " + text;
                if (seen.Add((pos, text)))
                    result.Add(new Sense(pos, text));
            }
        }

        return result;
    }

    /// <summary>
    /// 取一个短释义：有偏好词性先找那个词性；没有偏好时，首行是名词而后面有很短的形容词义的取形容词
    /// （sweet 名词义「糖果」排在前面，常用的却是「甜的」）。
    /// </summary>
    internal static string ShortGloss(string translation, IReadOnlyList<string>? prefer = null)
    {
        var senses = ShortSenses(translation);
        if (senses.Count == 0)
            return "";
        prefer ??= [];
        foreach (var (pos, text) in senses)
        {
            if (prefer.Contains(pos))
                return text;
        }

        if (prefer.Count == 0 && senses[0].Pos == "n.")
        {
            foreach (var (pos, text) in senses)
            {
                if (pos == "a." && text.Length <= 3)
                    return text;
            }
        }

        return senses[0].Text;
    }

    /// <summary>exchange 形如 "0:go/1:p"：0 是原形，1 是本词相对原形的变形类别。</summary>
    private static (string? Lemma, string Kinds) Exchange(string? exchange)
    {
        string? lemma = null;
        var kinds = "";
        foreach (var item in (exchange ?? "").Split('/'))
        {
            if (item.StartsWith("0:", StringComparison.Ordinal))
                lemma = item[2..];
            else if (item.StartsWith("1:", StringComparison.Ordinal))
                kinds = item[2..];
        }

        return (lemma, kinds);
    }

    private string GlossOf(string word, IReadOnlyList<string>? prefer = null)
    {
        if (Glosses.Common.TryGetValue(word, out var gloss))
            return gloss;
        var row = Row(word);
        return row is null ? "" : ShortGloss((string?)row[2] ?? "", prefer);
    }

    private DictEntry? Entry(string w)
    {
        var row = Row(w);
        if (row is null)
            return Glosses.Common.TryGetValue(w, out var common) ? new DictEntry(w, "", common, "") : null;
        var key = w;
        var tr = (string?)row[2] ?? "";
        var (lemma, kinds) = Exchange((string?)row[3]);
        var baseRow = lemma is null ? null : Row(lemma);
        string gloss;
        if (baseRow is not null && kinds.IndexOfAny(['p', 'd', 'i', '3']) >= 0)
        {
            key = lemma!;
            tr = (string?)baseRow[2] ?? "";
            gloss = GlossOf(lemma!, Verb);
        }
        else if (baseRow is not null && kinds.Contains('s'))
        {
            key = lemma!;
            tr = (string?)baseRow[2] ?? "";
            gloss = GlossOf(lemma!, Noun);
        }
        else
        {
            gloss = Glosses.Common.TryGetValue(w, out var common) && common.Length > 0 ? common : ShortGloss(tr);
        }

        var refers = RefersRx().Match(gloss);
        if (refers.Success && refers.Groups[1].Value != w)
        {
            key = refers.Groups[1].Value;
            gloss = GlossOf(key, Verb);
        }

        return gloss.Length == 0 ? null : new DictEntry(key, (string?)row[1] ?? "", gloss, tr);
    }

    private static string Normalize(string word) => word.ToLowerInvariant().Replace('’', '\'');

    /// <summary>查一个词。变形词归到原形，学会状态记在 lemma 上。</summary>
    public DictEntry? Lookup(string word)
    {
        if (!Installed)
            return null;
        var w = Normalize(word);
        if (w.EndsWith("n't", StringComparison.Ordinal))
        {
            var b = w[..^3];
            var g = GlossOf(Glosses.NotBases.TryGetValue(b, out var nb) ? nb : b, Verb);
            return g.Length > 0 ? new DictEntry(w, "", "不" + g, "") : null;
        }

        foreach (var (tail, tailGloss) in Glosses.ContractionTails)
        {
            if (w.EndsWith(tail, StringComparison.Ordinal) && w.Length > tail.Length)
            {
                var g = GlossOf(w[..^tail.Length]);
                return g.Length > 0 ? new DictEntry(w, "", g + tailGloss, "") : null;
            }
        }

        return Entry(w);
    }

    /// <summary>最小词形还原；学会的词不需要取中文。</summary>
    public string Lemma(string word)
    {
        var w = Normalize(word);
        if (w.EndsWith("n't", StringComparison.Ordinal) || Glosses.ContractionTails.Any(t => w.EndsWith(t.Tail, StringComparison.Ordinal)))
            return w;
        if (w is "am" or "is" or "are" or "was" or "were" or "been" or "being")
            return "be";
        if (!Installed)
            return w;
        var db = Db;
        object?[]? row;
        lock (db.Gate)
            row = db.QueryOne("SELECT exchange FROM words WHERE word = ?", w);
        if (row is not null)
        {
            var (lemma, kinds) = Exchange((string?)row[0]);
            if (lemma is not null && kinds.IndexOfAny(['p', 'd', 'i', '3', 's']) >= 0)
            {
                lock (db.Gate)
                {
                    if (db.QueryOne("SELECT 1 FROM words WHERE word = ?", lemma) is not null)
                        return lemma;
                }
            }
        }

        return w;
    }

    /// <summary>预测候选必须能查到释义；单字母只收 a / I。</summary>
    public bool IsWord(string word)
    {
        if (!Installed || (word.Length == 1 && word.ToLowerInvariant() is not ("a" or "i")))
            return false;
        return Lookup(word) is not null;
    }

    /// <summary>以 prefix 开头的常用词，按词频（frq 是排名，越小越常用）。</summary>
    public IReadOnlyList<string> Complete(string prefix, int n)
    {
        if (!Installed || prefix.Length == 0 || !prefix.All(char.IsLetter))
            return [];
        var db = Db;
        lock (db.Gate)
        {
            return db.Query("SELECT word FROM words WHERE word LIKE ? AND frq > 0 AND word NOT LIKE '% %'"
                            + " AND word NOT LIKE '%-%' ORDER BY frq LIMIT ?", prefix + "%", n)
                .Select(r => (string)r[0]!).ToList();
        }
    }

    /// <summary>INPUT-007：中文 → 英文候选（有界反查，整义项优先于子串命中）。</summary>
    public IReadOnlyList<string> EnglishChoices(string chinese)
    {
        if (!Installed)
            return [];
        if (_choices.TryGetValue(chinese, out var cached))
            return cached;
        var escaped = chinese.Replace("\\", "\\\\").Replace("%", "\\%").Replace("_", "\\_");
        var db = Db;
        List<object?[]> rows;
        lock (db.Gate)
        {
            rows = db.Query("SELECT word, translation, frq FROM words WHERE translation LIKE ? ESCAPE '\\'"
                            + " ORDER BY CASE WHEN frq > 0 THEN frq ELSE 999999 END, word LIMIT 256", "%" + escaped + "%");
        }

        var ranked = new List<(bool NotExact, long Frequency, string Word)>();
        foreach (var r in rows)
        {
            var word = (string)r[0]!;
            var translation = (string?)r[1] ?? "";
            var frequency = r[2] is long f ? f : 0;
            if (translation.Contains("中文拼音") || translation.Contains("汉语拼音"))
                continue;
            if (!ChoiceWordRx().IsMatch(word) || word.Length > 48)
                continue;
            var exact = SenseCandidates(translation).Any(item => ParenRx().Replace(item.Text, "").Trim(' ', '!', '！', '?', '？', '。') == chinese);
            ranked.Add((!exact, frequency > 0 ? frequency : 999999, word));
        }

        ranked.AddRange(Glosses.Common.Where(kv => kv.Value == chinese).Select(kv => (false, 0L, kv.Key)));
        var result = new List<string>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var item in ranked.OrderBy(x => x.NotExact).ThenBy(x => x.Frequency).ThenBy(x => x.Word, StringComparer.Ordinal))
        {
            if (seen.Add(item.Word))
                result.Add(item.Word);
            if (result.Count == 20)
                break;
        }

        if (_choices.Count > 256)
            _choices.Clear();
        _choices[chinese] = result;
        return result;
    }

    public Dictionary<string, DictEntry?> LookupMany(IEnumerable<string> words)
    {
        var result = new Dictionary<string, DictEntry?>(StringComparer.Ordinal);
        if (!Installed)
            return result;
        foreach (var w in words)
            result.TryAdd(w, Lookup(w));
        return result;
    }

    /// <summary>这个词形实际适用的完整义项，标签由调用方按出现位置分配。</summary>
    public IReadOnlyList<Sense> Senses(string word)
    {
        var entry = Lookup(word);
        if (entry is null)
            return [];
        var senses = SenseCandidates(entry.Translation);
        if (senses.Count == 0 && entry.Gloss.Length > 0)
            senses = [new Sense("", entry.Gloss)];
        return senses;
    }

    public HashSet<string> LearnedLemmas()
    {
        lock (_user.Gate)
            return _user.Query("SELECT lemma FROM learned").Select(r => (string)r[0]!).ToHashSet(StringComparer.Ordinal);
    }

    public List<(string Lemma, string Gloss)> Learned()
    {
        List<object?[]> rows;
        lock (_user.Gate)
            rows = _user.Query("SELECT lemma FROM learned ORDER BY learned_at DESC");
        return rows.Select(r => (string)r[0]!).Select(lemma => (lemma, Lookup(lemma)?.Gloss ?? "")).ToList();
    }

    public void SetLearned(string lemma, bool learned)
    {
        lock (_user.Gate)
        {
            if (learned)
                _user.Execute("INSERT OR REPLACE INTO learned VALUES (?, ?)", lemma, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0);
            else
                _user.Execute("DELETE FROM learned WHERE lemma = ?", lemma);
        }
    }

    /// <summary>把 ecdict.csv 转成 SQLite，只留查词要用的列。返回条数。</summary>
    public static int Build(string csvPath, string dbPath)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(dbPath)!);
        var tmp = Path.ChangeExtension(dbPath, ".tmp");
        File.Delete(tmp);
        var rows = 0;
        using (var db = new SqliteConnection(tmp))
        {
            db.Script("CREATE TABLE words (word TEXT PRIMARY KEY COLLATE NOCASE, phonetic TEXT,"
                      + " translation TEXT, exchange TEXT, frq INTEGER, tag TEXT); BEGIN;");
            using var reader = new StreamReader(csvPath, Encoding.UTF8);
            var header = Csv.ReadRecord(reader) ?? throw new InvalidDataException("CSV 是空的");
            int Col(string name) => Array.IndexOf(header, name) is var i and >= 0 ? i : throw new InvalidDataException($"CSV 缺列 {name}");
            int cw = Col("word"), cp = Col("phonetic"), ct = Col("translation"), ce = Col("exchange"), cf = Col("frq"), cg = Col("tag");
            while (Csv.ReadRecord(reader) is { } r)
            {
                if (r.Length <= Math.Max(Math.Max(cw, ct), Math.Max(cf, cg)))
                    continue;
                var tr = r[ct].Replace("\\r", "").Replace("\\n", "\n").Trim();
                if (tr.Length == 0)
                    continue;
                var frq = long.TryParse(r[cf], out var f) ? f : 0;
                rows += db.Execute("INSERT OR IGNORE INTO words VALUES (?,?,?,?,?,?)", r[cw], r[cp], tr, r[ce], frq, r[cg]);
            }

            db.Script("COMMIT;");
        }

        File.Move(tmp, dbPath, overwrite: true);
        return rows;
    }
}

/// <summary>RFC 4180 风格的 CSV 读取：引号字段里可以有逗号、换行与 "" 转义。</summary>
internal static class Csv
{
    public static string[]? ReadRecord(TextReader reader)
    {
        var fields = new List<string>();
        var field = new StringBuilder();
        var quoted = false;
        var any = false;
        while (true)
        {
            var c = reader.Read();
            if (c < 0)
            {
                if (!any)
                    return null;
                fields.Add(field.ToString());
                return [.. fields];
            }

            any = true;
            var ch = (char)c;
            if (quoted)
            {
                if (ch == '"')
                {
                    if (reader.Peek() == '"')
                    {
                        reader.Read();
                        field.Append('"');
                    }
                    else
                    {
                        quoted = false;
                    }
                }
                else
                {
                    field.Append(ch);
                }
            }
            else if (ch == '"')
            {
                quoted = true;
            }
            else if (ch == ',')
            {
                fields.Add(field.ToString());
                field.Clear();
            }
            else if (ch == '\n')
            {
                fields.Add(field.ToString().TrimEnd('\r'));
                return [.. fields];
            }
            else
            {
                field.Append(ch);
            }
        }
    }
}
