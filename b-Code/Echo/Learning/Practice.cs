using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Echo.Learning;

/// <summary>
/// 练词对话：从导入的单词表里挑几个词，整段对话都围着它们转。练词要求写进系统提示词（回复与输入预测共用）；
/// 「练过」只算学习者自己说出口的，AI 用了不算。练词计划是 JSON：<c>{list_id, label, words:[{word, meaning}]}</c>。
/// </summary>
internal static partial class Practice
{
    private const string PromptTemplate =
        "This conversation is vocabulary practice. The learner is practising these target words: {0}. "
        + "Pick a natural, everyday topic where these words fit, and stay with them for the whole conversation. "
        + "In each reply, use one or two target words naturally, and ask a question that invites the learner "
        + "to use a target word in their answer. {1}"
        + "Do not list the words or explain them unless the learner asks.";

    public const string Opening = "(The learner is ready. Start the conversation now, using one of the target words.)";

    [GeneratedRegex(@"[A-Za-z]+(?:['’][A-Za-z]+)*")] private static partial Regex TokenRx();

    public static List<string> WordsOf(JsonObject? plan)
        => (plan?["words"] as JsonArray ?? [])
            .OfType<JsonObject>()
            .Select(w => w["word"]?.GetValue<string>())
            .Where(w => w is not null)
            .Select(w => w!)
            .ToList();

    public static string Prompt(JsonObject? plan, ISet<string> used)
    {
        var words = WordsOf(plan);
        if (words.Count == 0)
            return "";
        var left = words.Where(w => !used.Contains(w)).ToList();
        var focus = left.Count > 0
            ? $"The learner has not used these yet, so steer towards them first: {string.Join(", ", left)}. "
            : "The learner has already used every target word; keep reviewing them in new contexts. ";
        return " " + string.Format(PromptTemplate, string.Join(", ", words), focus);
    }

    /// <summary>学习者在这些消息里用上了哪些目标词：单词按原形比（说 went 也算练了 go），词组按整段原文找。</summary>
    public static HashSet<string> UsedWords(JsonObject? plan, IEnumerable<ChatTurn> messages, Func<string, DictEntry?> lookup)
    {
        var words = WordsOf(plan);
        var text = string.Join(" ", messages.Where(m => m.Role == "user").Select(m => m.Content));
        var used = new HashSet<string>(StringComparer.Ordinal);
        if (words.Count == 0 || text.Length == 0)
            return used;
        var low = text.ToLowerInvariant().Replace('’', '\'');
        var said = new HashSet<string>(StringComparer.Ordinal);
        foreach (Match tok in TokenRx().Matches(low))
        {
            said.Add(tok.Value);
            if (lookup(tok.Value) is { } entry)
                said.Add(entry.Lemma);
        }

        foreach (var w in words)
        {
            var key = w.ToLowerInvariant();
            if (key.Contains(' ') || key.Contains('-'))
            {
                if (Regex.IsMatch(low, $@"\b{Regex.Escape(key)}\b"))
                    used.Add(w);
                continue;
            }

            var e = lookup(key);
            if (said.Contains(key) || (e is not null && said.Contains(e.Lemma)))
                used.Add(w);
        }

        return used;
    }
}
