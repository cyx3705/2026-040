using System.Net;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Echo.Inference;
using Echo.Learning;
using Echo.Web;

namespace Echo.App;

internal sealed partial class EchoApp
{
    private const string TitlePrompt =
        "下面是一段英语口语练习对话。给它起一个简短的中文标题，4 到 10 个字，概括聊的话题。"
        + "只输出标题本身，不要引号、不要标点、不要解释。\n\n{0}";

    internal static JsonObject? EntryJson(DictEntry? entry) => entry is null ? null : new JsonObject
    {
        ["lemma"] = entry.Lemma, ["phonetic"] = entry.Phonetic, ["gloss"] = entry.Gloss, ["translation"] = entry.Translation,
    };

    internal static List<ChatTurn> Turns(JsonNode? node)
        => (node as JsonArray ?? []).Select(ChatTurn.FromJson).ToList();

    /// <summary>难度和长度要求，加上练词要求（还没用上的词排在前面）。</summary>
    internal string SystemFor(string? level, int length, JsonObject? plan, IReadOnlyList<ChatTurn> messages)
    {
        var used = Practice.UsedWords(plan, messages, Dictionary.Lookup);
        return Levels.SystemPrompt(level, length) + Practice.Prompt(plan, used);
    }

    private JsonObject Lookup(JsonObject body)
    {
        var words = (body["words"] as JsonArray ?? []).Select(w => w?.GetValue<string>() ?? "").Take(2000);
        var result = new JsonObject();
        foreach (var (word, entry) in Dictionary.LookupMany(words))
            result[word] = EntryJson(entry);
        return result;
    }

    private JsonObject SetLearned(JsonObject body)
    {
        var lemma = body["lemma"]?.GetValue<string>() ?? throw new HttpError(422, "缺少 lemma");
        var learned = body["learned"]?.GetValueKind() == JsonValueKind.True;
        Dictionary.SetLearned(lemma.ToLowerInvariant(), learned);
        return new JsonObject { ["ok"] = true };
    }

    private JsonObject Conversation(long id)
    {
        var conv = Store.Get(id) ?? throw new HttpError(404, "没有这段对话");
        var plan = conv["practice"] as JsonObject;
        var used = Practice.UsedWords(plan, Turns(conv["messages"]), Dictionary.Lookup).Order(StringComparer.Ordinal);
        conv["level"] = Levels.Normalize(conv["level"]?.GetValue<string>());
        conv["used"] = new JsonArray(used.Select(u => (JsonNode)JsonValue.Create(u)!).ToArray());
        return conv;
    }

    private JsonObject WordList(long id)
    {
        var list = Store.WordList(id) ?? throw new HttpError(404, "没有这个单词表");
        foreach (var w in (list["words"] as JsonArray ?? []).OfType<JsonObject>())
        {
            // 文档里没写释义的，用词典的短释义补上
            var word = w["word"]!.GetValue<string>();
            if (string.IsNullOrEmpty(w["meaning"]?.GetValue<string>()) && !word.Contains(' '))
                w["meaning"] = Dictionary.Lookup(word)?.Gloss ?? "";
        }

        return list;
    }

    /// <summary>页面把 .docx 原样发过来，文件名放在 x-filename 头里（URL 编码）。</summary>
    private async Task<JsonObject> ImportWordListAsync(EchoRequest request)
    {
        var header = WebUtility.UrlDecode(request.Header("x-filename") ?? "单词表");
        var dot = header.LastIndexOf('.');
        var name = dot > 0 ? header[..dot] : header;
        if (string.IsNullOrEmpty(name))
            name = "单词表";
        List<WordItem> items;
        try
        {
            items = WordLists.ParseDocx(await request.BodyAsync().ConfigureAwait(false));
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            throw new HttpError(400, "读不了这个文件：只支持 Word 的 .docx 格式，老的 .doc 请先在 Word 里另存为 .docx");
        }

        if (items.Count == 0)
            throw new HttpError(400, "这个文档里没找到单词：要每段一个词，或者表格里一行一个词");
        var id = Store.AddWordList(name, items);
        return new JsonObject { ["id"] = id, ["name"] = name, ["count"] = items.Count };
    }

    /// <summary>自动命名：用当前模型按前几轮内容起个中文标题。已经起过的不再改。</summary>
    internal async Task<JsonObject> NameConversationAsync(long id, CancellationToken cancellation)
    {
        var conv = Store.Get(id) ?? throw new HttpError(404, "没有这段对话");
        if (conv["titled"]?.GetValue<bool>() == true)
            return new JsonObject { ["title"] = conv["title"]?.GetValue<string>() };
        var transcript = string.Join("\n", Turns(conv["messages"]).Take(4)
            .Select(m => $"{(m.Role == "user" ? "Learner" : "Echo")}: {m.Content}"));
        var model = CurrentModel;
        var raw = new System.Text.StringBuilder();
        using (await Gate.UseAsync(cancellation).ConfigureAwait(false))
        {
            await Gate.Slot.WaitAsync(cancellation).ConfigureAwait(false);
            try
            {
                using (await Gate.RequestAsync(cancellation).ConfigureAwait(false))
                {
                    await foreach (var piece in Inference.ChatAsync(model, "Return a concise Chinese title only.",
                                       [new ChatTurn("user", string.Format(TitlePrompt, transcript))], 40, cancellation).ConfigureAwait(false))
                        raw.Append(piece);
                }
            }
            catch (InferenceException ex)
            {
                throw new HttpError(503, ex.Message);
            }
            finally
            {
                Gate.Slot.Release();
            }
        }

        var title = CleanTitle(raw.ToString());
        if (title.Length > 0)
            Store.SetTitle(id, title);
        return new JsonObject { ["title"] = title.Length > 0 ? title : conv["title"]?.GetValue<string>() };
    }

    internal static string CleanTitle(string raw)
    {
        var line = raw.Trim().Split('\n').Select(s => s.Trim()).FirstOrDefault(s => s.Length > 0) ?? "";
        line = Regex.Replace(line, @"^(标题|Title)\s*[:：]\s*", "", RegexOptions.IgnoreCase);
        line = line.Trim(' ', '\t', '"', '\'', '“', '”', '‘', '’', '《', '》', '「', '」', '*', '#', '。', '.', '!', '！');
        return line.Length > 16 ? line[..16] : line;
    }

    /// <summary>只有本页能改本机键盘模式：校验来源，必须是回环地址上的同源页面。</summary>
    private async Task<JsonObject> InputModeAsync(EchoRequest request)
    {
        if (!HttpHost.IsLoopbackHost(request.Host) || request.Header("Origin") != request.Origin)
            throw new HttpError(403, "input_origin_mismatch");
        var body = await request.JsonAsync().ConfigureAwait(false);
        var action = body["action"]?.GetValue<string>();
        if (action is not ("acquire" or "confirm" or "refresh" or "release"))
            throw new HttpError(422, "action 须为 acquire/confirm/refresh/release");
        var marker = body["marker"]?.GetValue<string>() ?? "";
        if (marker.Length > 0 && !Regex.IsMatch(marker, "^[0-9a-f]{32}$"))
            throw new HttpError(422, "marker 格式错误");
        var owner = body["owner"]?.GetValueKind() == JsonValueKind.Number ? body["owner"]!.GetValue<ulong>() : 0;
        try
        {
            if (action == "acquire")
            {
                if (marker.Length == 0)
                    throw new HttpError(422, "input_marker_required");
                return await Task.Run(() => Input.Acquire(marker)).ConfigureAwait(false);
            }

            if (body["bounds"] is JsonObject bounds && action is "confirm" or "refresh")
            {
                double D(string key, double min, double max)
                {
                    var value = bounds[key]?.GetValue<double>() ?? throw new HttpError(422, $"bounds 缺 {key}");
                    if (!double.IsFinite(value) || value < min || value > max)
                        throw new HttpError(422, $"bounds.{key} 越界");
                    return value;
                }

                var values = new[]
                {
                    D("left", 0, 32768), D("top", 0, 32768), D("width", 0, 32768), D("height", 0, 32768),
                    D("viewport_width", 0, 32768), D("viewport_height", 0, 32768), D("font_size", 8, 128),
                };
                var color = (uint)D("color", 0, 0xffffff);
                var background = (uint)D("background", 0, 0xffffff);
                await Task.Run(() => Input.Bounds(owner, values, color, background)).ConfigureAwait(false);
            }

            if (action == "refresh")
                return await Task.Run(() => Input.Refresh(owner)).ConfigureAwait(false);
            if (action == "confirm")
                return await Task.Run(() => Input.Confirm(owner)).ConfigureAwait(false);
            await Task.Run(() => Input.Release(owner)).ConfigureAwait(false);
            return new JsonObject { ["active"] = false };
        }
        catch (InvalidOperationException ex)
        {
            throw new HttpError(409, ex.Message);
        }
    }
}
