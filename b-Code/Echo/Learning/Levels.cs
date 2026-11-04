using System.Text.Json.Nodes;

namespace Echo.Learning;

/// <summary>
/// 对话难度：按学习者熟悉的评价标准选（CEFR、四六级、专四专八、雅思口语小分），都折算到 CEFR 档位套说话规则。
/// 难度只改 AI 说话的方式，写进系统提示词；输入预测共用同一份系统提示词。
/// </summary>
internal static class Levels
{
    private static readonly Dictionary<string, string> Style = new()
    {
        ["A1"] = "Use only the few hundred most common English words and the present simple, "
                 + "and keep every sentence under eight words.",
        ["A2"] = "Use simple everyday words, basic tenses and short sentences.",
        ["B1"] = "Use common everyday English with a few phrasal verbs, in clear sentences.",
        ["B2"] = "Use natural English with a wider vocabulary, phrasal verbs and some common idioms.",
        ["C1"] = "Speak fluent, natural English with precise vocabulary, idioms and varied sentence structures.",
        ["C2"] = "Speak like an educated native speaker: nuanced vocabulary, idioms, subtle humour and complex "
                 + "sentences where natural.",
        ["C2+"] = "Speak exactly as a witty, articulate native speaker would with a friend: rich idioms, slang, "
                  + "cultural references and wordplay, with no simplification at all.",
    };

    /// <summary>回答长度（页面滑块）。0 是「自动」，按难度档位取默认长度。</summary>
    public static readonly (string Name, string Note)[] Lengths =
    [
        ("自动", "跟着难度走：入门一两句，高级一小段"),
        ("一句", "只说一句，而且是个问题"),
        ("简短", "一两句，问一个问题"),
        ("适中", "三四句，再接着问"),
        ("详细", "一整段：说想法、举例子"),
        ("长篇", "两三段：展开讲、给细节和观点"),
    ];

    private static readonly Dictionary<int, string> LengthRules = new()
    {
        [1] = "Your whole reply must be a single question sentence that keeps the conversation going. "
              + "Do not start with a reaction such as \"That sounds fun!\" or any other sentence.",
        [2] = "Reply in at most two short sentences in total, counting any short reaction such as \"Nice!\", "
              + "and end with a question.",
        [3] = "Reply in three or four sentences, and end with a follow-up question.",
        [4] = "Reply in one full paragraph of five to seven sentences: share your own thoughts, an example "
              + "or a short story, then ask a follow-up question.",
        [5] = "Reply at length, in two or three paragraphs: explain, give examples and details, share your "
              + "opinions, then ask a follow-up question.",
    };

    private static readonly Dictionary<string, int> AutoLength = new()
    {
        ["A1"] = 2, ["A2"] = 2, ["B1"] = 3, ["B2"] = 3, ["C1"] = 4, ["C2"] = 4, ["C2+"] = 4,
    };

    private const string Exams = "China's College English Test";
    private const string Tem = "China's Test for English Majors";

    private sealed record Level(string Id, string Name, string Band, string Note, string? English);

    private static readonly (string Id, string Name, Level[] Items)[] Standards =
    [
        ("cefr", "欧洲语言框架",
        [
            new("cefr:A1", "A1", "A1", "入门：几百个最常用的词", null),
            new("cefr:A2", "A2", "A2", "基础：日常简单话题", null),
            new("cefr:B1", "B1", "B1", "进阶：熟悉话题能连贯表达", null),
            new("cefr:B2", "B2", "B2", "中高级：和母语者自然交流", null),
            new("cefr:C1", "C1", "C1", "高级：流利、用词精确", null),
            new("cefr:C2", "C2", "C2", "精通：接近受过教育的母语者", null),
            new("cefr:C2+", "C2+", "C2+", "母语：俚语、梗、文字游戏都来", null),
        ]),
        ("cn", "国内考试",
        [
            new("cet:4", "CET-4", "B1", "四级：约 4500 词，≈ B1", $"{Exams} Band 4 (CET-4)"),
            new("cet:6", "CET-6", "B2", "六级：约 6000 词，≈ B2", $"{Exams} Band 6 (CET-6)"),
            new("tem:4", "专四", "B2", "TEM-4：约 8000 词，≈ B2+", $"{Tem} Band 4 (TEM-4)"),
            new("tem:8", "专八", "C1", "TEM-8：约 13000 词，≈ C1", $"{Tem} Band 8 (TEM-8)"),
        ]),
        ("ielts", "雅思口语",
            new[] { ("4.0", "A2"), ("4.5", "B1"), ("5.0", "B1"), ("5.5", "B2"), ("6.0", "B2"), ("6.5", "B2"),
                    ("7.0", "C1"), ("7.5", "C1"), ("8.0", "C1"), ("8.5", "C2"), ("9.0", "C2") }
                .Select(x => new Level($"ielts:{x.Item1}", x.Item1, x.Item2, $"口语 {x.Item1} 分，≈ {x.Item2}", $"IELTS Speaking band {x.Item1}"))
                .ToArray()),
    ];

    public const string Default = "cefr:A2";

    /// <summary>上一版只有四档：旧对话里存的难度按这张表折算。</summary>
    private static readonly Dictionary<string, string> Legacy = new()
    {
        ["starter"] = "cefr:A1", ["basic"] = "cefr:A2", ["intermediate"] = "cefr:B1", ["advanced"] = "cefr:C1",
    };

    private static readonly Dictionary<string, Level> ById =
        Standards.SelectMany(s => s.Items).ToDictionary(l => l.Id, StringComparer.Ordinal);

    public static string Normalize(string? level)
    {
        level ??= Default;
        if (Legacy.TryGetValue(level, out var mapped))
            level = mapped;
        return ById.ContainsKey(level) ? level : Default;
    }

    /// <summary>扁平的难度候选（Aurora 选项框用）：id 与显示名，雅思带「雅思」前缀。</summary>
    public static IEnumerable<(string Id, string Label)> Choices()
        => Standards.SelectMany(s => s.Items.Select(l => (l.Id, s.Id == "ielts" ? "雅思 " + l.Name : l.Name)));

    public static JsonArray Catalog() => new(Standards.Select(s => (JsonNode)new JsonObject
    {
        ["id"] = s.Id,
        ["name"] = s.Name,
        ["levels"] = new JsonArray(s.Items.Select(l => (JsonNode)new JsonObject
        {
            ["id"] = l.Id, ["name"] = l.Name, ["band"] = l.Band, ["note"] = l.Note,
        }).ToArray()),
    }).ToArray());

    public static JsonArray LengthCatalog() => new(Lengths.Select((l, i) => (JsonNode)new JsonObject
    {
        ["id"] = i, ["name"] = l.Name, ["note"] = l.Note,
    }).ToArray());

    /// <summary>提示词里的难度和长度要求。</summary>
    public static string Describe(string? level, int length = 0)
    {
        var lv = ById[Normalize(level)];
        var who = lv.English is null
            ? $"The learner's English is at CEFR level {lv.Name}."
            : $"The learner's English is at the level of {lv.English} (roughly CEFR {lv.Band}).";
        var size = LengthRules.ContainsKey(length) ? length : AutoLength[lv.Band];
        return $"{who} Match that level: {Style[lv.Band]} {LengthRules[size]}";
    }

    public static string SystemPrompt(string? level, int length = 0)
        => "You are Echo, a patient English conversation partner for a Chinese learner. "
           + "Reply naturally in English. " + Describe(level, length);
}
