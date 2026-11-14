using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;
using Echo.App;
using Echo.Learning;
using HistoryVulcan.Core.Commands;

namespace Echo.Board;

/// <summary>
/// Aurora 页面背后的状态：会话卡片与气泡行集、一轮进行中的对话（草稿）、按节拍刷新时间线。
/// 对话本身走 <see cref="EchoApp.RunChatAsync"/>，与网页同一条路，说完才存档。
/// </summary>
internal sealed class EchoBoard : IDisposable
{
    /// <summary>生成中刷新时间线的最短间隔。</summary>
    internal static TimeSpan Beat { get; set; } = TimeSpan.FromMilliseconds(350);

    private const string LevelSetting = "aurora_level";
    private const string LengthSetting = "aurora_length";
    private const string StandardSetting = "aurora_standard";

    private readonly Func<CancellationToken, Task<EchoApp>> _app;
    private readonly ICommandBus _bus;
    private readonly Action<string> _log;
    private readonly object _gate = new();
    private Draft? _draft;
    private long _currentConversation;

    public EchoBoard(Func<CancellationToken, Task<EchoApp>> app, ICommandBus bus, Action<string> log)
    {
        _app = app;
        _bus = bus;
        _log = log;
    }

    /// <summary>一轮进行中的对话：用户这句、已生成的回复与已选出的释义。</summary>
    private sealed class Draft(long conversation, string user)
    {
        public long Conversation { get; } = conversation;
        public string User { get; } = user;
        public StringBuilder Reply { get; } = new();
        public Dictionary<string, JsonObject> Glosses { get; } = new(StringComparer.Ordinal);
        public CancellationTokenSource Cancel { get; } = new();
        public TaskCompletionSource<string> Finished { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public string? Error { get; set; }
        public bool Running { get; set; } = true;
        public bool Dirty { get; set; }
    }

    // ---- 难度 ----

    public static string LevelLabel(string id)
        => Levels.Choices().FirstOrDefault(c => c.Id == id).Label ?? Levels.Choices().First().Label;

    public async Task<string> LevelAsync(CancellationToken cancellation)
    {
        var app = await _app(cancellation).ConfigureAwait(false);
        return Levels.Normalize(app.Store.Setting(LevelSetting, Levels.Default));
    }

    /// <summary>按显示名或 id 设难度；认不出返回 null。</summary>
    public async Task<string?> SetLevelAsync(string value, CancellationToken cancellation)
    {
        value = value.Trim();
        var hit = Levels.Choices().FirstOrDefault(c => c.Label == value || c.Id == value);
        if (hit.Id is null)
            return null;
        var app = await _app(cancellation).ConfigureAwait(false);
        app.Store.SetSetting(LevelSetting, hit.Id);
        return hit.Label;
    }

    public async Task<int> LengthAsync(CancellationToken cancellation)
    {
        var app = await _app(cancellation).ConfigureAwait(false);
        return int.TryParse(app.Store.Setting(LengthSetting, "2"), out var value) && value is >= 1 and <= 4 ? value : 2;
    }

    public async Task<string?> SetLengthAsync(string value, CancellationToken cancellation)
    {
        value = value.Trim();
        var hit = Levels.Lengths.Select((x, i) => (Name: x.Name, Number: i))
            .FirstOrDefault(x => x.Name.Equals(value, StringComparison.OrdinalIgnoreCase)
                || x.Number.ToString(CultureInfo.InvariantCulture) == value);
        if (hit.Name is null)
            return null;
        var app = await _app(cancellation).ConfigureAwait(false);
        app.Store.SetSetting(LengthSetting, (hit.Number + 1).ToString(CultureInfo.InvariantCulture));
        return hit.Name;
    }

    public static IReadOnlyList<IReadOnlyDictionary<string, string>> StandardRows()
        => new[]
        {
            new Dictionary<string, string> { ["value"] = "欧洲语言框架" },
            new Dictionary<string, string> { ["value"] = "国内考试" },
            new Dictionary<string, string> { ["value"] = "雅思口语" },
        };

    public async Task<string?> SetStandardAsync(string value, CancellationToken cancellation)
    {
        value = value.Trim();
        var hit = StandardRows().FirstOrDefault(x => x["value"].Equals(value, StringComparison.OrdinalIgnoreCase));
        if (hit is null)
            return null;
        var app = await _app(cancellation).ConfigureAwait(false);
        app.Store.SetSetting(StandardSetting, value);
        return value;
    }

    public static IReadOnlyList<IReadOnlyDictionary<string, string>> LengthRows()
        => Levels.Lengths.Select(x => (IReadOnlyDictionary<string, string>)new Dictionary<string, string> { ["value"] = x.Name }).ToList();

    public async Task<JsonArray> SuggestAsync(long conversation, string text, CancellationToken cancellation)
    {
        var app = await _app(cancellation).ConfigureAwait(false);
        var conv = app.Store.Get(conversation) ?? throw new InvalidOperationException("没有这段对话");
        var body = new JsonObject { ["messages"] = new JsonArray(EchoApp.Turns(conv["messages"]).Select(m => (JsonNode)m.ToJson()).ToArray()), ["text"] = text, ["candidates"] = 20 };
        return await app.SuggestForAuroraAsync(body, cancellation).ConfigureAwait(false);
    }

    public async Task<IReadOnlyList<IReadOnlyDictionary<string, string>>> SuggestRowsAsync(long conversation, string text, CancellationToken cancellation)
    {
        var rows = new List<IReadOnlyDictionary<string, string>>();
        foreach (var item in await SuggestAsync(conversation, text, cancellation).ConfigureAwait(false))
        {
            var word = item?["word"]?.GetValue<string>() ?? "";
            if (word.Length == 0) continue;
            var gloss = item?["gloss"]?.GetValue<string>() ?? "";
            rows.Add(new Dictionary<string, string>
            {
                ["value"] = word,
                ["text"] = gloss.Length == 0 ? word : word + " · " + gloss,
            });
        }
        return rows;
    }

    public async Task<string> AcceptSuggestionAsync(string text, string word, CancellationToken cancellation)
    {
        text ??= "";
        word = word.Trim();
        if (word.Length == 0) return text;
        var partial = System.Text.RegularExpressions.Regex.Match(text, "[A-Za-z']+$").Value;
        var prefix = partial.Length == 0 ? text : text[..^partial.Length];
        var value = prefix + word + " ";
        await _bus.InvokeAsync("aurora.ui.panelset panel=" + EchoPage.SendPanelId + " control=" + EchoPage.TextBoxId + " value=" + CommandParser.QuoteArg(value), "Echo", cancellation).ConfigureAwait(false);
        return value;
    }

    public async Task<JsonObject> TranslateAsync(long conversation, string chinese, CancellationToken cancellation)
    {
        var app = await _app(cancellation).ConfigureAwait(false);
        var conv = app.Store.Get(conversation) ?? throw new InvalidOperationException("没有这段对话");
        var body = new JsonObject { ["messages"] = new JsonArray(EchoApp.Turns(conv["messages"]).Select(m => (JsonNode)m.ToJson()).ToArray()), ["chinese"] = chinese };
        return await app.TranslateForAuroraAsync(body, cancellation).ConfigureAwait(false);
    }

    public async Task<IReadOnlyList<IReadOnlyDictionary<string, string>>> DictionaryRowsAsync(CancellationToken cancellation)
    {
        var app = await _app(cancellation).ConfigureAwait(false);
        return app.Dictionary.Learned().Select(w => (IReadOnlyDictionary<string, string>)new Dictionary<string, string>
        {
            ["lemma"] = w.Lemma, ["gloss"] = w.Gloss,
        }).ToList();
    }

    public static IReadOnlyList<IReadOnlyDictionary<string, string>> LevelRows()
        => Levels.Choices().Select(c => (IReadOnlyDictionary<string, string>)new Dictionary<string, string> { ["value"] = c.Label }).ToList();

    // ---- 行集 ----

    public async Task<IReadOnlyList<IReadOnlyDictionary<string, string>>> ConversationRowsAsync(string? q, CancellationToken cancellation)
    {
        var app = await _app(cancellation).ConfigureAwait(false);
        var running = _draft is { Running: true } d ? d.Conversation : (long?)null;
        var rows = new List<IReadOnlyDictionary<string, string>>();
        foreach (var c in app.Store.List(q ?? "").OfType<JsonObject>())
        {
            var id = c["id"]!.GetValue<long>();
            var snippet = c["snippet"]?.GetValue<string>() is { Length: > 0 } s ? s : c["last"]?.GetValue<string>() ?? "";
            rows.Add(new Dictionary<string, string>
            {
                ["id"] = id.ToString(CultureInfo.InvariantCulture),
                ["title"] = c["title"]?.GetValue<string>() ?? "",
                ["last"] = id == running ? "正在回复…" : snippet,
                ["source"] = "",
                ["kind"] = "",
                ["when"] = When(c["updated"]?.GetValue<double>() ?? 0),
                ["unread"] = "0",
            });
        }

        return rows;
    }

    internal static string When(double unixSeconds)
    {
        if (unixSeconds <= 0)
            return "";
        var t = DateTimeOffset.FromUnixTimeMilliseconds((long)(unixSeconds * 1000)).ToLocalTime();
        var now = DateTimeOffset.Now;
        return t.Date == now.Date ? t.ToString("HH:mm", CultureInfo.InvariantCulture)
            : t.Year == now.Year ? t.ToString("M-d HH:mm", CultureInfo.InvariantCulture)
            : t.ToString("yyyy-M-d", CultureInfo.InvariantCulture);
    }

    public async Task<IReadOnlyList<JsonObject>> MessageRowsAsync(long conversation, CancellationToken cancellation)
    {
        _currentConversation = conversation;
        var app = await _app(cancellation).ConfigureAwait(false);
        var rows = new List<JsonObject>();
        var conv = app.Store.Get(conversation);
        var stored = EchoApp.Turns(conv?["messages"]);
        for (var i = 0; i < stored.Count; i++)
            rows.Add(Row("m" + i, stored[i].Role, stored[i].Content, stored[i].Glosses?.OfType<JsonObject>()));

        Draft? draft;
        lock (_gate)
            draft = _draft?.Conversation == conversation ? _draft : null;
        if (draft is not null)
        {
            string reply;
            List<JsonObject> glosses;
            lock (_gate)
            {
                reply = draft.Reply.ToString();
                glosses = draft.Glosses.Values.OrderBy(g => g["start"]?.GetValue<int>() ?? 0).ToList();
            }

            rows.Add(Row("d" + stored.Count, "user", draft.User, null));
            var text = reply.Length > 0 ? reply : draft.Running ? "…" : "";
            if (draft.Error is { } error)
                text = (text.Length > 0 ? text + "\n\n" : "") + "（这一轮没说完，不进历史：" + error + "）";
            rows.Add(Row("d" + (stored.Count + 1), "assistant", text, glosses));
        }

        return rows;

        JsonObject Row(string id, string role, string content, IEnumerable<JsonObject>? glosses)
        {
            var user = role == "user";
            var row = new JsonObject
            {
                ["id"] = id,
                ["who"] = user ? "我" : "Echo",
                ["time"] = "",
                ["text"] = content,
                ["direction"] = user ? "out" : "in",
            };
            if (!user && glosses is not null)
            {
                var learned = app.Dictionary.LearnedLemmas();
                row["words"] = new JsonArray(glosses
                    .Where(g => g["word"]?.GetValue<string>() is { Length: > 0 })
                    .Where(g => g["state"]?.GetValue<string>() != "missing")
                    .Select(g =>
                    {
                        var copy = (JsonObject)g.DeepClone();
                        var word = copy["word"]?.GetValue<string>() ?? "";
                        var lemma = copy["lemma"]?.GetValue<string>() ?? "";
                        var known = copy["state"]?.GetValue<string>() == "skipped_learned" || lemma.Length > 0 && learned.Contains(lemma);
                        copy["learned"] = known;
                        if (known && string.IsNullOrWhiteSpace(copy["gloss"]?.GetValue<string>()) && word.Length > 0)
                            copy["gloss"] = app.Dictionary.Lookup(word)?.Gloss ?? "";
                        return (JsonNode)copy;
                    }).ToArray());
            }
            return row;
        }
    }

    public long CurrentConversation => _currentConversation;

    public async Task OpenDictionaryAsync(CancellationToken cancellation)
    {
        _ = await _bus.InvokeAsync("aurora.ui.show page=" + EchoPage.DictionaryPageId, "Echo", cancellation).ConfigureAwait(false);
    }

    public async Task CloseDictionaryAsync(CancellationToken cancellation)
    {
        _ = await _bus.InvokeAsync("aurora.ui.hide page=" + EchoPage.DictionaryPageId, "Echo", cancellation).ConfigureAwait(false);
    }

    /// <summary>气泡底下的释义行：按出现顺序、同一个词只列一次，学会的和查不到的不列。</summary>
    internal static string GlossLine(IEnumerable<JsonObject>? glosses, IReadOnlySet<string> learned)
    {
        if (glosses is null)
            return "";
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var parts = new List<string>();
        foreach (var g in glosses)
        {
            var word = g["word"]?.GetValue<string>() ?? "";
            var gloss = g["gloss"]?.GetValue<string>() ?? "";
            var lemma = g["lemma"]?.GetValue<string>() ?? "";
            if (word.Length == 0 || gloss.Length == 0 || g["state"]?.GetValue<string>() == "skipped_learned" || learned.Contains(lemma))
                continue;
            if (seen.Add(word))
                parts.Add(word + " " + gloss);
        }

        return parts.Count == 0 ? "" : "释义：" + string.Join(" · ", parts);
    }

    // ---- 动作 ----

    public async Task<long> NewConversationAsync(CancellationToken cancellation)
    {
        var app = await _app(cancellation).ConfigureAwait(false);
        var id = app.Store.NewConversation(await LevelAsync(cancellation).ConfigureAwait(false));
        _currentConversation = id;
        await RefreshAsync(EchoPage.ConversationListId).ConfigureAwait(false);
        await RefreshAsync(EchoPage.MessageListId).ConfigureAwait(false);
        return id;
    }

    /// <summary>删除一段对话；正在回复的那段不让删。</summary>
    public async Task<bool> DeleteAsync(long id, CancellationToken cancellation)
    {
        if (_draft is { Running: true } d && d.Conversation == id)
            throw new InvalidOperationException("这段对话正在回复，先停止再删");
        var app = await _app(cancellation).ConfigureAwait(false);
        if (app.Store.Get(id) is null)
            return false;
        app.Store.Delete(id);
        lock (_gate)
        {
            if (_draft?.Conversation == id)
                _draft = null;
        }

        await RefreshAsync(EchoPage.ConversationListId).ConfigureAwait(false);
        return true;
    }

    public bool Stop()
    {
        var d = _draft;
        if (d is not { Running: true })
            return false;
        d.Cancel.Cancel();
        return true;
    }

    /// <summary>
    /// 开一轮对话并立即返回结束任务（结果是 AI 的完整回复）；同一时间只跑一轮。
    /// 用户这句和生成中的回复先作为草稿显示，说完存档后草稿撤掉。
    /// </summary>
    public async Task<Task<string>> SendAsync(long conversation, string text, CancellationToken cancellation)
    {
        text = text.Trim();
        if (text.Length == 0)
            throw new InvalidOperationException("先写一句再发送");
        var app = await _app(cancellation).ConfigureAwait(false);
        var conv = app.Store.Get(conversation) ?? throw new InvalidOperationException($"没有编号 {conversation} 的对话");
        var draft = new Draft(conversation, text);
        lock (_gate)
        {
            if (_draft is { Running: true })
                throw new InvalidOperationException("上一句还在回复，等它说完或先停止");
            _draft = draft;
        }

        var level = await LevelAsync(cancellation).ConfigureAwait(false);
        var messages = (conv["messages"] as JsonArray ?? []).DeepClone().AsArray();
        messages.Add(new JsonObject { ["role"] = "user", ["content"] = text });
        var body = new JsonObject
        {
            ["messages"] = messages, ["level"] = level, ["length"] = await LengthAsync(cancellation).ConfigureAwait(false), ["conversation_id"] = conversation,
            ["practice"] = conv["practice"]?.DeepClone(),
        };
        var fresh = messages.Count == 1 && conv["titled"]?.GetValue<bool>() != true && conv["practice"] is null;
        _ = Task.Run(() => RunAsync(app, draft, body, fresh));
        await RefreshAsync(EchoPage.MessageListId, EchoPage.ConversationListId).ConfigureAwait(false);
        return draft.Finished.Task;
    }

    private async Task RunAsync(EchoApp app, Draft draft, JsonObject body, bool fresh)
    {
        var token = draft.Cancel.Token;
        var saved = false;
        using var beat = new PeriodicTimer(Beat);
        var ticker = Task.Run(async () =>
        {
            try
            {
                while (await beat.WaitForNextTickAsync(CancellationToken.None).ConfigureAwait(false) && draft.Running)
                {
                    bool dirty;
                    lock (_gate)
                    {
                        dirty = draft.Dirty;
                        draft.Dirty = false;
                    }

                    if (dirty)
                        await RefreshAsync(EchoPage.MessageListId).ConfigureAwait(false);
                }
            }
            catch (ObjectDisposedException)
            {
            }
        });

        try
        {
            await app.RunChatAsync(body, payload =>
            {
                lock (_gate)
                {
                    if (payload["delta"]?.GetValue<string>() is { } delta)
                        draft.Reply.Append(delta);
                    if (payload["gloss"] is JsonArray items)
                    {
                        foreach (var item in items.OfType<JsonObject>())
                            draft.Glosses[$"{item["start"]}:{item["end"]}:{item["word"]}"] = (JsonObject)item.DeepClone();
                    }

                    if (payload["error"]?.GetValue<string>() is { } error)
                        draft.Error = error;
                    if (payload["done"]?.GetValueKind() == System.Text.Json.JsonValueKind.True)
                    {
                        saved = true;
                        if (ReferenceEquals(_draft, draft))
                            _draft = null;
                    }

                    draft.Dirty = true;
                }

                return Task.CompletedTask;
            }, token).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            draft.Error = ex.Message;
            _log($"Aurora 对话失败：{ex.GetType().Name}: {ex.Message}");
        }
        finally
        {
            if (!saved && draft.Error is null)
                draft.Error = token.IsCancellationRequested ? "已停止" : "没有完成";
            draft.Running = false;
            beat.Dispose();
            await ticker.ConfigureAwait(false);
        }

        await RefreshAsync(EchoPage.MessageListId, EchoPage.ConversationListId).ConfigureAwait(false);
        if (saved)
            draft.Finished.TrySetResult(draft.Reply.ToString());
        else
            draft.Finished.TrySetException(new InvalidOperationException(draft.Error ?? "没有完成"));

        if (saved && fresh)
        {
            try
            {
                await app.NameConversationAsync(draft.Conversation, CancellationToken.None).ConfigureAwait(false);
                await RefreshAsync(EchoPage.ConversationListId, EchoPage.MessageListId).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                _log($"自动起标题失败：{ex.Message}");
            }
        }
    }

    /// <summary>请 Aurora 重取节点；Aurora 没装或没开页面时安静失败。</summary>
    private async Task RefreshAsync(params string[] nodes)
    {
        foreach (var node in nodes)
        {
            try
            {
                await _bus.InvokeAsync("aurora.ui.refreshdata node=" + node, "", CancellationToken.None).ConfigureAwait(false);
            }
            catch (Exception)
            {
            }
        }
    }

    public void Dispose()
    {
        _draft?.Cancel.Cancel();
    }
}
