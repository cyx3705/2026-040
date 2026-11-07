using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Echo.Data;
using Echo.Inference;
using Echo.Learning;
using Echo.Native;
using Echo.Web;

namespace Echo.App;

/// <summary>
/// Echo 后端：页面与扩展的全部 HTTP 接口（与原 Python 版 echo-serve 同路径、同 JSON 形状），推理一律经 <see cref="IInference"/>。
/// </summary>
internal sealed partial class EchoApp : IDisposable
{
    internal const string DefaultModel = "qwen3.5:35b-a3b-q4_K_M";

    private readonly IUserDb _userDb;
    private readonly Dictionary<string, string> _families = new(StringComparer.Ordinal);
    private readonly object _familyGate = new();
    private readonly Action<string> _log;

    /// <param name="paths">数据位置。</param>
    /// <param name="inference">推理入口。</param>
    /// <param name="log">日志。</param>
    /// <param name="bus">经 Diana 访问 z 里的 user.db 时要用；user.db 写成本机绝对路径（测试）时可省。</param>
    public EchoApp(EchoPaths paths, IInference inference, Action<string> log, HistoryVulcan.Core.Commands.ICommandBus? bus = null)
    {
        Paths = paths;
        Inference = inference;
        _log = log;
        // 0.6.0：user.db 是 z 资产，生产经 diana.z.* 由源码区的 Diana 持连接；Echo 自己不打开它。
        _userDb = Path.IsPathFullyQualified(paths.UserDatabase)
            ? new LocalUserDb(paths.UserDatabase)
            : new ZUserDb(bus ?? throw new InvalidOperationException("user.db 在 z 里，要经 diana.z 访问，但没有命令总线"), paths.UserDatabase);
        Dictionary = new Learning.Dictionary(paths.DictionaryDatabase, _userDb);
        Store = new Store(_userDb);
        Input = new InputBridge(paths.NativeDirectory);
        Contexts = new ReaderContexts(paths.ReaderContextDirectory);
        Gate.SetParallel(int.TryParse(Store.Setting("engine_parallel", "1"), out var p) && p is 1 or 2 ? p : 1);
        var env = Environment.GetEnvironmentVariable("ECHO_MODEL");
        if (!string.IsNullOrWhiteSpace(env))
            Store.SetSetting("model", env.Trim());
    }

    public EchoPaths Paths { get; }

    public IInference Inference { get; }

    public Learning.Dictionary Dictionary { get; }

    public Store Store { get; }

    public InputBridge Input { get; }

    public ReaderContexts Contexts { get; }

    public EngineGate Gate { get; } = new();

    public string CurrentModel => Store.Setting("model", Environment.GetEnvironmentVariable("ECHO_MODEL") ?? DefaultModel);

    public void Dispose()
    {
        Input.Dispose();
        Dictionary.Dispose();
        _userDb.Dispose();
    }

    /// <summary>路由表。</summary>
    public async Task HandleAsync(EchoRequest request)
    {
        var path = request.Path;
        var method = request.Method;
        var r = request.Response;
        switch (method, path)
        {
            case ("GET", "/"):
                await HttpHost.WriteBytesAsync(r, 200, "text/html; charset=utf-8", Encoding.UTF8.GetBytes(IndexHtml())).ConfigureAwait(false);
                return;
            case ("GET", _) when path.StartsWith("/static/", StringComparison.Ordinal):
                await StaticAsync(request, path["/static/".Length..]).ConfigureAwait(false);
                return;
            case ("GET", "/api/status"):
                await Reply(r, await StatusAsync(request.Aborted).ConfigureAwait(false));
                return;
            case ("POST", "/api/debug"):
                await Reply(r, await SetDebugAsync(await request.JsonAsync().ConfigureAwait(false), request.Aborted).ConfigureAwait(false));
                return;
            case ("GET", "/api/models"):
                await Reply(r, await ModelsAsync(request.Aborted).ConfigureAwait(false));
                return;
            case ("POST", "/api/model"):
                await Reply(r, await SetModelAsync(await request.JsonAsync().ConfigureAwait(false), request.Aborted).ConfigureAwait(false));
                return;
            case ("POST", "/api/lookup"):
                await Reply(r, Lookup(await request.JsonAsync().ConfigureAwait(false)));
                return;
            case ("POST", "/api/gloss"):
                await Reply(r, await GlossAsync(await request.JsonAsync().ConfigureAwait(false), request.Aborted).ConfigureAwait(false));
                return;
            case ("POST", "/api/suggest"):
                await Reply(r, await SuggestAsync(await request.JsonAsync().ConfigureAwait(false), request.Aborted).ConfigureAwait(false));
                return;
            case ("POST", "/api/input-translation"):
                await Reply(r, await TranslateInputAsync(await request.JsonAsync().ConfigureAwait(false), request.Aborted).ConfigureAwait(false));
                return;
            case ("POST", "/api/input-mode"):
                await Reply(r, await InputModeAsync(request).ConfigureAwait(false));
                return;
            case ("GET", "/api/learned"):
                await Reply(r, new JsonArray(Dictionary.Learned().Select(x => (JsonNode)new JsonObject { ["lemma"] = x.Lemma, ["gloss"] = x.Gloss }).ToArray()));
                return;
            case ("GET", "/api/reader/learned"):
                await Reply(r, new JsonArray(Dictionary.LearnedLemmas().Order(StringComparer.Ordinal).Select(l => (JsonNode)new JsonObject { ["lemma"] = l }).ToArray()));
                return;
            case ("POST", "/api/learned"):
                await Reply(r, SetLearned(await request.JsonAsync().ConfigureAwait(false)));
                return;
            case ("POST", "/api/reader/gloss"):
                await ReaderGlossAsync(request).ConfigureAwait(false);
                return;
            case ("POST", "/api/chat"):
                await ChatAsync(request).ConfigureAwait(false);
                return;
            case ("GET", "/api/conversations"):
                await Reply(r, Store.List(request.Query("q") ?? ""));
                return;
            case ("GET", "/api/wordlists"):
                await Reply(r, Store.WordLists());
                return;
            case ("POST", "/api/wordlists"):
                await Reply(r, await ImportWordListAsync(request).ConfigureAwait(false));
                return;
        }

        var conv = ConversationRoute().Match(path);
        if (conv.Success && long.TryParse(conv.Groups[1].Value, out var convId))
        {
            var title = conv.Groups[2].Success;
            if (method == "GET" && !title)
            {
                await Reply(r, Conversation(convId));
                return;
            }

            if (method == "DELETE" && !title)
            {
                Store.Delete(convId);
                await Reply(r, new JsonObject { ["ok"] = true });
                return;
            }

            if (method == "POST" && title)
            {
                await Reply(r, await NameConversationAsync(convId, request.Aborted).ConfigureAwait(false));
                return;
            }
        }

        var list = WordListRoute().Match(path);
        if (list.Success && long.TryParse(list.Groups[1].Value, out var listId))
        {
            if (method == "GET")
            {
                await Reply(r, WordList(listId));
                return;
            }

            if (method == "DELETE")
            {
                Store.DeleteWordList(listId);
                await Reply(r, new JsonObject { ["ok"] = true });
                return;
            }
        }

        throw new HttpError(404, "Not Found");
    }

    [GeneratedRegex(@"^/api/conversations/(\d+)(/title)?$")] private static partial Regex ConversationRoute();
    [GeneratedRegex(@"^/api/wordlists/(\d+)$")] private static partial Regex WordListRoute();

    private static Task Reply(System.Net.HttpListenerResponse response, JsonNode? body) => HttpHost.WriteJsonAsync(response, 200, body);

    // ---- 静态页面：随包嵌入；HTML 里的资源地址带内容摘要，新 HTML 不会配上旧 CSS/JS ----

    internal static byte[]? Resource(string name)
    {
        using var stream = typeof(EchoApp).Assembly.GetManifestResourceStream("static/" + name);
        if (stream is null)
            return null;
        using var buffer = new MemoryStream();
        stream.CopyTo(buffer);
        return buffer.ToArray();
    }

    internal static string IndexHtml()
    {
        var html = Encoding.UTF8.GetString(Resource("index.html") ?? throw new HttpError(500, "模块包缺少 index.html"));
        foreach (var name in new[] { "app.css", "app.js", "composer_input.js" })
        {
            var bytes = Resource(name);
            if (bytes is null)
                continue;
            var version = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant()[..16];
            html = Regex.Replace(html, "\"/static/" + Regex.Escape(name) + "(?:\\?[^\"\\s]*)?\"", $"\"/static/{name}?v={version}\"");
        }

        return html.StartsWith('\uFEFF') ? html[1..] : html;
    }

    private static async Task StaticAsync(EchoRequest request, string name)
    {
        if (name.Contains('/') || name.Contains('\\') || name.Contains(".."))
            throw new HttpError(404, "Not Found");
        var bytes = Resource(name) ?? throw new HttpError(404, "Not Found");
        var type = Path.GetExtension(name) switch
        {
            ".css" => "text/css; charset=utf-8",
            ".js" => "text/javascript; charset=utf-8",
            ".html" => "text/html; charset=utf-8",
            ".svg" => "image/svg+xml",
            ".png" => "image/png",
            _ => "application/octet-stream",
        };
        await HttpHost.WriteBytesAsync(request.Response, 200, type, bytes).ConfigureAwait(false);
    }

    internal void Log(string message) => _log(message);
}
