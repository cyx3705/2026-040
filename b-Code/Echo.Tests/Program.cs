using System.Diagnostics;
using System.IO.Compression;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Echo;
using Echo.App;
using Echo.Inference;
using Echo.Learning;
using Echo.Tests;
using Echo.Web;
using HistoryVulcan.Core.Commands;

Console.OutputEncoding = Encoding.UTF8;

// 全部离线：推理用假的（FakeInference / FakeBus），词典与用户库建在临时目录；最后把打好的包交给已发布宿主的 --probe 装一遍。
var tests = new (string Name, Func<Task> Run)[]
{
    ("command registration", Sync(TestCommandRegistration)),
    ("versions agree", Sync(TestVersionsAgree)),
    ("dictionary lookup and lemma", Sync(TestDictionary)),
    ("sense candidates and short gloss", Sync(TestSenses)),
    ("english choices", Sync(TestEnglishChoices)),
    ("store roundtrip", Sync(TestStore)),
    ("levels prompt", Sync(TestLevels)),
    ("practice used words", Sync(TestPractice)),
    ("word list docx", Sync(TestWordLists)),
    ("prompt templates", Sync(TestTemplates)),
    ("suggest filters tokens", TestSuggestAsync),
    ("ratio commands quoted", TestRatioCommandsAsync),
    ("stalled stream gives up", TestStalledStreamAsync),
    ("static index versioned", Sync(TestIndex)),
    ("http status and models", TestHttpStatusAsync),
    ("http suggest and lookup", TestHttpSuggestAsync),
    ("http chat plain", () => TestHttpChatAsync(shared: false)),
    ("http chat shared kv", () => TestHttpChatAsync(shared: true)),
    ("http reader gloss", TestHttpReaderAsync),
    ("http conversations and wordlists", TestHttpLibraryAsync),
    ("http model switch", TestHttpModelSwitchAsync),
    ("http input origin check", TestHttpInputOriginAsync),
    ("share key", Sync(TestShareKey)),
    ("input bridge sources", Sync(TestInputSources)),
    ("host loads the package", TestHostLoadsPackage),
};

var failed = 0;
foreach (var test in tests)
{
    try
    {
        await test.Run();
        Console.WriteLine($"PASS {test.Name}");
    }
    catch (Exception ex)
    {
        failed++;
        Console.Error.WriteLine($"FAIL {test.Name}: {ex.Message}");
    }
}

return failed == 0 ? 0 : 1;

static Func<Task> Sync(Action test) => () =>
{
    test();
    return Task.CompletedTask;
};

static void TestCommandRegistration()
{
    var registry = new Registrar();
    EchoModule.Register(registry, new EchoService(new FakeBus(_ => CommandResult.Ok()), _ => { }));
    string[] expected = ["app.status", "app.start", "app.stop", "app.open", "dict.setup", "input.build", "word.list", "word.learn"];
    foreach (var name in expected)
    {
        True(registry.TryGet("echo." + name, out var d), $"未注册 echo.{name}");
        Equal("echo", d!.Domain!);
        True(!string.IsNullOrWhiteSpace(d.Example), $"echo.{name} 缺示例");
    }

    Equal(expected.Length, registry.All.Count);
    True(registry.TryGet("echo.app.status", out var status) && status!.Readonly, "status 只读");
    True(registry.TryGet("echo.word.learn", out var learn) && !learn!.Readonly, "learn 改数据");
}

static void TestVersionsAgree()
{
    using var document = JsonDocument.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "module.manifest.json")));
    var manifest = document.RootElement;
    Equal(EchoIdentity.Name, manifest.GetProperty("name").GetString()!);
    Equal(new ModuleInfo().Version, manifest.GetProperty("version").GetString()!);
    Equal(EchoIdentity.Description, manifest.GetProperty("description").GetString()!);
    True(manifest.GetProperty("dependsOn").EnumerateArray().Any(x => x.GetString() == "Ratio"), "要依赖 Ratio");
}

static void TestDictionary()
{
    using var ws = new Workspace();
    using var app = new EchoApp(ws.Paths, new FakeInference(), _ => { });
    var d = app.Dictionary;
    True(d.Installed, "词典应已建好");
    Equal("go", d.Lookup("went")!.Lemma);
    Equal("去", d.Lookup("went")!.Gloss);
    Equal("go", d.Lemma("went"));
    Equal("make", d.Lemma("making"));
    Equal("be", d.Lemma("Was"));
    Equal("喜欢", d.Lookup("like")!.Gloss);
    Equal("不能", d.Lookup("can't")!.Gloss);
    Equal("那是", d.Lookup("that’s")!.Gloss);
    True(d.IsWord("bank") && !d.IsWord("x") && d.IsWord("a"), "is_word");
    Equal("make,making", string.Join(",", d.Complete("mak", 5)));
    d.SetLearned("go", true);
    True(d.LearnedLemmas().Contains("go"), "学会记录");
    d.SetLearned("go", false);
    True(!d.LearnedLemmas().Contains("go"), "取消学会");
}

static void TestSenses()
{
    var senses = Echo.Learning.Dictionary.SenseCandidates("n. 银行, 河岸\nv. 存款");
    Equal("n.:银行|n.:河岸|v.:存款", string.Join("|", senses.Select(s => s.Pos + ":" + s.Text)));
    Equal("甜的", Echo.Learning.Dictionary.ShortGloss("n. 糖果\na. 甜的"));
    Equal("糖果", Echo.Learning.Dictionary.ShortGloss("n. 糖果\na. 甜的", ["n."]));
    Equal("去", Echo.Learning.Dictionary.ShortGloss("[计] 术语\nv. 去(go的过去式), 走"));
    var bracket = Echo.Learning.Dictionary.SenseCandidates("n. 点(一种, 二种), 线");
    Equal(2, bracket.Count);
}

static void TestEnglishChoices()
{
    using var ws = new Workspace();
    using var app = new EchoApp(ws.Paths, new FakeInference(), _ => { });
    var choices = app.Dictionary.EnglishChoices("苹果");
    Equal("apple", choices[0]);
    Equal("你好", Glosses.Common["hi"]);
    True(app.Dictionary.EnglishChoices("你好").Contains("hello"), "反查要含高频表");
}

static void TestStore()
{
    using var ws = new Workspace();
    using var app = new EchoApp(ws.Paths, new FakeInference(), _ => { });
    var glosses = new JsonArray(new JsonObject { ["word"] = "Hi" });
    var id = app.Store.Save(null, [new ChatTurn("user", "I like apples"), new ChatTurn("assistant", "Hi!", glosses)], "cefr:A2");
    var conv = app.Store.Get(id)!;
    Equal("I like apples", conv["title"]!.GetValue<string>());
    Equal(2, conv["messages"]!.AsArray().Count);
    Equal("Hi", conv["messages"]![1]!["glosses"]![0]!["word"]!.GetValue<string>());
    Equal(1, app.Store.List("apple").Count);
    True(app.Store.List("apple")[0]!["snippet"]!.GetValue<string>().Contains("apples"), "搜索片段");
    app.Store.SetTitle(id, "苹果");
    Equal(true, app.Store.Get(id)!["titled"]!.GetValue<bool>());
    var list = app.Store.AddWordList("U1", [new WordItem("go", "去", "L1"), new WordItem("bank", "", "L1")]);
    app.Store.CountUsed(list, ["go"]);
    Equal(1L, app.Store.WordLists()[0]!["practiced"]!.GetValue<long>());
    app.Store.SetSetting("model", "m");
    Equal("m", app.Store.Setting("model", "x"));
    app.Store.Delete(id);
    True(app.Store.Get(id) is null, "删对话");
}

static void TestLevels()
{
    Equal("The learner's English is at the level of China's College English Test Band 4 (CET-4) (roughly CEFR B1). Match that level: "
          + "Use common everyday English with a few phrasal verbs, in clear sentences. Reply in three or four sentences, and end with a follow-up question.",
        Levels.Describe("cet:4"));
    Equal("cefr:A1", Levels.Normalize("starter"));
    Equal(Levels.Default, Levels.Normalize("nope"));
    Equal(3, Levels.Catalog().Count);
    Equal(6, Levels.LengthCatalog().Count);
}

static void TestPractice()
{
    using var ws = new Workspace();
    using var app = new EchoApp(ws.Paths, new FakeInference(), _ => { });
    var plan = JsonNode.Parse("""{"list_id":1,"label":"U1","words":[{"word":"go"},{"word":"river bank"},{"word":"apple"}]}""")!.AsObject();
    var used = Practice.UsedWords(plan, [new ChatTurn("user", "I went to the river bank"), new ChatTurn("assistant", "apple")], app.Dictionary.Lookup);
    Equal("go,river bank", string.Join(",", used.Order()));
    True(Practice.Prompt(plan, used).Contains("steer towards them first: apple"), "未用词优先");
}

static void TestWordLists()
{
    var data = Docx.Make(Docx.P("L1 1-1") + Docx.P("spacious 宽敞的") + Docx.P("Terrorism &amp; Conflict")
                         + "<w:tbl><w:tr><w:tc>" + Docx.P("单词 Word") + "</w:tc></w:tr><w:tr><w:tc>" + Docx.P("abandon")
                         + "</w:tc><w:tc>" + Docx.P("/əˈbændən/") + "</w:tc><w:tc>" + Docx.P("放弃") + "</w:tc></w:tr></w:tbl>"
                         + Docx.P("Spacious"));
    var items = WordLists.ParseDocx(data);
    Equal(2, items.Count);
    Equal("spacious|宽敞的|L1 1-1", $"{items[0].Word}|{items[0].Meaning}|{items[0].Group}");
    Equal("abandon|放弃", $"{items[1].Word}|{items[1].Meaning}");
    True(WordLists.Entry("I like it.") is null, "英文句子不是词条");
}

static void TestTemplates()
{
    Equal("chatml", Prompts.TemplateOf("qwen3"));
    Equal("gemma4", Prompts.TemplateOf("gemma4"));
    Equal(null, Prompts.TemplateOf("llama"));
    var prompt = Prompts.Render("chatml", "S", [new ChatTurn("user", "Hi"), new ChatTurn("assistant", "Yo")], "I");
    Equal("<|im_start|>system\nS<|im_end|>\n<|im_start|>user\nHi<|im_end|>\n<|im_start|>assistant\nYo<|im_end|>\n<|im_start|>user\nI", prompt);
    Equal("<start_of_turn>user\nS\n\nHi", Prompts.Render("gemma", "S", [], "Hi"));
    var assistant = Prompts.AssistantPrompt("chatml", "S", [new ChatTurn("user", "Hi")]);
    True(assistant.EndsWith("<|im_start|>assistant\n<think>\n\n</think>\n\n"), "A 前缀停在 assistant 回合");
    Equal("{\"a\": [1, \"中\"], \"b\": null}", PyJson.Dumps(JsonNode.Parse("{\"a\":[1,\"中\"],\"b\":null}")));
    Equal("新的对话", EchoApp.CleanTitle("标题：《新的对话》。\n多余"));
}

static async Task TestSuggestAsync()
{
    using var ws = new Workspace();
    using var app = new EchoApp(ws.Paths, new FakeInference(), _ => { });
    IReadOnlyList<TokenCandidate> tops =
    [
        new(" go", -0.1), new(" Go", -0.2), new("ing", -0.3), new(" .", -0.4), new(" bank", -0.5), new(" xq", -0.6), new("\n", -0.7),
    ];
    var next = await Prompts.SuggestAsync("chatml", "S", [], "I ", 5, (_, _) => Task.FromResult(tops), app.Dictionary.Complete, app.Dictionary.IsWord);
    Equal("go,.,bank", string.Join(",", next.Select(s => s.Word)));
    Equal("punct", next[1].Kind);
    var partial = await Prompts.SuggestAsync("chatml", "S", [], "I Ma", 3, (prompt, _) =>
    {
        True(prompt.EndsWith("user\nI"), "补全时退回词首");
        return Task.FromResult<IReadOnlyList<TokenCandidate>>([new(" make", -0.1)]);
    }, app.Dictionary.Complete, app.Dictionary.IsWord);
    Equal("Make,Making", string.Join(",", partial.Select(s => s.Word)));
    Equal("dict", partial[1].Kind);
}

static async Task TestRatioCommandsAsync()
{
    var bus = new FakeBus(text => text.Split(' ')[0] switch
    {
        "ratio.local.probs" => CommandResult.Ok("", """{"tokens":[{"token":" hi","logprob":-1.5}]}"""),
        "ratio.chat.stream" => CommandResult.Ok("", """{"stream":"s1"}"""),
        "ratio.stream.read" when text.Contains("after=0") => CommandResult.Ok("", """{"text":"He","next":2,"done":false,"error":null}"""),
        "ratio.stream.read" => CommandResult.Ok("", """{"text":"llo","next":5,"done":true,"error":null}"""),
        "ratio.seq.open" => CommandResult.Fail("shared_kv:invalid_sequence_size"),
        "ratio.chat.send" => CommandResult.Ok("", """{"content":"{\"label\":\"A\"}","finishReason":"stop"}"""),
        _ => CommandResult.Fail("unknown"),
    });
    var inference = new RatioInference(bus);
    var tops = await inference.TopTokensAsync("say \"hi\"\nnow", 20, "qwen3:8b", CancellationToken.None);
    Equal(" hi", tops[0].Token);
    var parsed = CommandParser.Parse(bus.Commands[0]);
    Equal("say \"hi\"\nnow", parsed.Named["prompt"]);
    var text = new StringBuilder();
    await foreach (var piece in inference.ChatAsync("m", "sys", [new ChatTurn("user", "x")], 64, CancellationToken.None))
        text.Append(piece);
    Equal("Hello", text.ToString());
    True(CommandParser.Parse(bus.Commands[1]).Named["think"] == "false", "对话要关思考");
    var kv = await ThrowsAsync<KvUnsupportedException>(() => inference.OpenSequenceAsync("m", "p", CancellationToken.None));
    Equal("invalid_sequence_size", kv.Message);
    var answer = await inference.AskAsync("m", "q", 40, new JsonObject { ["type"] = "object" }, 0, CancellationToken.None);
    Equal("{\"label\":\"A\"}", answer);
    True(CommandParser.Parse(bus.Commands[^1]).Named.ContainsKey("schema"), "带 schema");
}

static async Task TestStalledStreamAsync()
{
    var bus = new FakeBus(text => text.Split(' ')[0] switch
    {
        "ratio.chat.stream" => CommandResult.Ok("", """{"stream":"s1"}"""),
        "ratio.stream.read" => CommandResult.Ok("", """{"text":"","next":0,"done":false,"error":null}"""),
        _ => CommandResult.Ok("", "{}"),
    });
    var old = RatioInference.StallLimit;
    RatioInference.StallLimit = TimeSpan.FromMilliseconds(200);
    try
    {
        var error = await ThrowsAsync<InferenceException>(async () =>
        {
            await foreach (var _ in new RatioInference(bus).ChatAsync("m", null, [new ChatTurn("user", "x")], 8, CancellationToken.None))
            {
            }
        });
        True(error.Message.Contains("两分钟"), error.Message);
        await Task.Delay(50);
        True(bus.Commands.Any(c => c.StartsWith("ratio.stream.cancel stream=s1", StringComparison.Ordinal)), "放弃时要取消流");
    }
    finally
    {
        RatioInference.StallLimit = old;
    }
}

static void TestIndex()
{
    var html = EchoApp.IndexHtml();
    True(System.Text.RegularExpressions.Regex.IsMatch(html, "/static/app\\.js\\?v=[0-9a-f]{16}\""), "app.js 带摘要");
    True(EchoApp.Resource("composer_input.js") is { Length: > 0 }, "嵌入 composer_input.js");
}

static async Task TestHttpStatusAsync()
{
    await using var server = await TestServer.StartAsync(new FakeInference());
    var status = await server.GetJsonAsync("/api/status");
    Equal("ready", status["state"]!.GetValue<string>());
    Equal("qwen3:8b", status["model"]!.GetValue<string>());
    Equal(false, status["kv"]!["supported"]!.GetValue<bool>());
    server.Fake.Running = false;
    Equal("stopped", (await server.GetJsonAsync("/api/status"))["state"]!.GetValue<string>());
    var (code, _) = await server.SendAsync(HttpMethod.Get, "/api/models");
    Equal(503, code);
    server.Fake.Running = true;
    var models = await server.GetJsonAsync("/api/models");
    Equal(2, models["models"]!.AsArray().Count);
    Equal(404, (await server.SendAsync(HttpMethod.Get, "/nope")).Status);
}

static async Task TestHttpSuggestAsync()
{
    var fake = new FakeInference { Tops = [new(" bank", -0.1), new(" .", -0.5)] };
    await using var server = await TestServer.StartAsync(fake);
    var (code, body) = await server.SendAsync(HttpMethod.Post, "/api/suggest", """{"messages":[],"text":"I go ","candidates":5}""");
    Equal(200, code);
    var items = JsonNode.Parse(body)!.AsArray();
    Equal("bank", items[0]!["word"]!.GetValue<string>());
    Equal("银行", items[0]!["gloss"]!.GetValue<string>());
    Equal("句号", items[1]!["gloss"]!.GetValue<string>());
    var lookup = JsonNode.Parse((await server.SendAsync(HttpMethod.Post, "/api/lookup", """{"words":["went","zzz"]}""")).Body)!;
    Equal("go", lookup["went"]!["lemma"]!.GetValue<string>());
    True(lookup["zzz"] is null, "查不到为 null");
    var gloss = JsonNode.Parse((await server.SendAsync(HttpMethod.Post, "/api/gloss", """{"text":"go bank","context":[],"default_only":true}""")).Body)!.AsArray();
    Equal("legacy_default", gloss[1]!["fallback_reason"]!.GetValue<string>());
}

static async Task TestHttpChatAsync(bool shared)
{
    var fake = new FakeInference { Shared = shared, Reply = ["I went", " to the", " bank."], Label = labels => labels[1] };
    fake.Answer = prompt => System.Text.RegularExpressions.Regex.Match(prompt, "\"occurrence_id\": \"(w[0-9]+)\"") is { Success: true } m
        ? new JsonObject { [m.Groups[1].Value] = "A" }.ToJsonString() : "{}";
    await using var server = await TestServer.StartAsync(fake);
    var (code, body) = await server.SendAsync(HttpMethod.Post, "/api/chat", """{"messages":[{"role":"user","content":"hi"}],"level":"cet:4"}""");
    Equal(200, code);
    var events = body.Split("\n\n", StringSplitOptions.RemoveEmptyEntries).Select(e => JsonNode.Parse(e["data: ".Length..])!.AsObject()).ToList();
    Equal("I went to the bank.", string.Concat(events.Where(e => e["delta"] is not null).Select(e => e["delta"]!.GetValue<string>())));
    var done = events.Single(e => e["done"] is not null);
    var glosses = done["glosses"]!.AsArray();
    var bank = glosses.Single(g => g!["word"]!.GetValue<string>() == "bank")!;
    Equal("银行", bank["gloss"]!.GetValue<string>());
    Equal("model", bank["source"]!.GetValue<string>());
    Equal(14, bank["start"]!.GetValue<int>());
    True(events.Any(e => e["text_done"] is not null) && events.Any(e => e["gloss_done"] is not null), "事件齐全");
    var conv = done["conversation_id"]!.GetValue<long>();
    var saved = await server.GetJsonAsync($"/api/conversations/{conv}");
    Equal(2, saved["messages"]!.AsArray().Count);
    if (shared)
    {
        True(fake.Calls.Contains("seqstream") && fake.Calls.Contains("choose"), "共享 KV 路径");
        Equal(0, fake.OpenSequences);
    }
    else
    {
        True(fake.Calls.Contains("chat") && fake.Calls.Contains("ask"), "普通路径");
    }
}

static async Task TestHttpReaderAsync()
{
    var fake = new FakeInference { Shared = true, Label = labels => labels[2] };
    fake.Reply = ["{\"0\": \"佐格\"}"];
    await using var server = await TestServer.StartAsync(fake);
    var (code, body) = await server.SendAsync(HttpMethod.Post, "/api/reader/gloss", """{"text":"The river bank and Zorg.","session":"web:x"}""");
    Equal(200, code);
    var events = body.Split("\n\n", StringSplitOptions.RemoveEmptyEntries).Select(e => JsonNode.Parse(e["data: ".Length..])!.AsObject()).ToList();
    Equal("dictionary", events[0]["phase"]!.GetValue<string>());
    var items = events.Skip(1).Where(e => e["items"] is not null).SelectMany(e => e["items"]!.AsArray()).ToList();
    Equal("河岸", items.Last(i => i!["word"]!.GetValue<string>() == "bank")!["gloss"]!.GetValue<string>());
    Equal("佐格", items.Last(i => i!["word"]!.GetValue<string>() == "Zorg")!["gloss"]!.GetValue<string>());
    True(events[^1]["done"] is not null, "以 done 结束");
    Equal(0, fake.OpenSequences);
    True(Directory.GetFiles(server.Workspace.Paths.ReaderContextDirectory).Length == 1, "存了伴读上下文");
}

static async Task TestHttpLibraryAsync()
{
    await using var server = await TestServer.StartAsync(new FakeInference { Reply = ["练习", "对话"] });
    var docx = Docx.Make(Docx.P("go 去") + Docx.P("bank"));
    using var content = new ByteArrayContent(docx);
    using var request = new HttpRequestMessage(HttpMethod.Post, server.Url + "/api/wordlists") { Content = content };
    request.Headers.Add("x-filename", Uri.EscapeDataString("我的单词.docx"));
    var response = await server.Client.SendAsync(request);
    var created = JsonNode.Parse(await response.Content.ReadAsStringAsync())!;
    Equal("我的单词", created["name"]!.GetValue<string>());
    var list = await server.GetJsonAsync($"/api/wordlists/{created["id"]}");
    Equal("银行", list["words"]![1]!["meaning"]!.GetValue<string>());
    Equal(400, (await server.SendAsync(HttpMethod.Post, "/api/wordlists", "not a docx")).Status);
    var id = server.App.Store.Save(null, [new ChatTurn("user", "hello")], "cefr:A2");
    var title = JsonNode.Parse((await server.SendAsync(HttpMethod.Post, $"/api/conversations/{id}/title")).Body)!;
    Equal("练习对话", title["title"]!.GetValue<string>());
    Equal(1, (await server.GetArrayAsync("/api/conversations?q=" + Uri.EscapeDataString("练习"))).Count);
    Equal(200, (await server.SendAsync(HttpMethod.Delete, $"/api/conversations/{id}")).Status);
    Equal(404, (await server.SendAsync(HttpMethod.Get, $"/api/conversations/{id}")).Status);
    await server.SendAsync(HttpMethod.Post, "/api/learned", """{"lemma":"Go","learned":true}""");
    Equal("go", (await server.GetArrayAsync("/api/reader/learned"))[0]!["lemma"]!.GetValue<string>());
}

static async Task TestHttpModelSwitchAsync()
{
    var fake = new FakeInference();
    await using var server = await TestServer.StartAsync(fake);
    var (code, body) = await server.SendAsync(HttpMethod.Post, "/api/model", """{"name":"gemma4:12b"}""");
    Equal(200, code);
    Equal("gemma4:12b", JsonNode.Parse(body)!["current"]!.GetValue<string>());
    True(fake.Calls.Contains("use gemma4:12b"), "经 ratio.local.use 换模");
    Equal(404, (await server.SendAsync(HttpMethod.Post, "/api/model", """{"name":"nope"}""")).Status);
    var debug = JsonNode.Parse((await server.SendAsync(HttpMethod.Post, "/api/debug", """{"candidates":7,"parallel":2}""")).Body)!;
    Equal(2, debug["parallel"]!.GetValue<int>());
}

static async Task TestHttpInputOriginAsync()
{
    await using var server = await TestServer.StartAsync(new FakeInference());
    using var request = new HttpRequestMessage(HttpMethod.Post, server.Url + "/api/input-mode")
    {
        Content = new StringContent("""{"action":"release"}""", Encoding.UTF8, "application/json"),
    };
    request.Headers.Add("Origin", "http://evil.example");
    var response = await server.Client.SendAsync(request);
    Equal(401, (int)response.StatusCode);
}

static void TestShareKey()
{
    var file = Path.Combine(Path.GetTempPath(), "Echo.Tests", Guid.NewGuid().ToString("N"), "key.txt");
    var key = HttpHost.LoadShareKey(file);
    Equal(key, HttpHost.LoadShareKey(file));
    True(key.Length >= 32, "密钥够长");
    Throws<InvalidOperationException>(() => HttpHost.Validate("short"));
    True(HttpHost.IsLoopbackHost("localhost") && HttpHost.IsLoopbackHost("127.0.0.1") && !HttpHost.IsLoopbackHost("10.0.0.2"), "回环判断");
}

static void TestInputSources()
{
    var sources = Echo.Native.InputBridge.LoadSources();
    Equal(4, sources.Count);
    var dir = Echo.Native.InputBridge.BuildDirectory(@"C:\x", sources);
    True(Path.GetFileName(dir).StartsWith("input-") && Path.GetFileName(dir).Length == 22, "构建目录名");
}

static async Task TestHostLoadsPackage()
{
    var root = Path.Combine(Path.GetTempPath(), "Echo.Tests", Guid.NewGuid().ToString("N"));
    var package = Path.Combine(root, EchoIdentity.Name);
    Directory.CreateDirectory(package);
    try
    {
        foreach (var file in new[] { "module.manifest.json", "Echo.dll", "Echo.xml" })
            File.Copy(Path.Combine(AppContext.BaseDirectory, file), Path.Combine(package, file));
        using var document = JsonDocument.Parse(File.ReadAllText(Path.Combine(package, "module.manifest.json")));
        var version = document.RootElement.GetProperty("version").GetString()!;
        File.WriteAllText(Path.Combine(package, "Echo.nuspec"), $"""
            <?xml version="1.0" encoding="utf-8"?>
            <package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">
              <metadata><id>Echo</id><version>{version}</version>
              <authors>HistoryVulcan</authors><description>Echo</description></metadata>
            </package>
            """, new UTF8Encoding(false));
        var archive = Path.Combine(root, "Echo.nupkg");
        ZipFile.CreateFromDirectory(package, archive);

        var hostRoot = Assembly.GetExecutingAssembly().GetCustomAttributes<AssemblyMetadataAttribute>()
            .First(item => item.Key == "HistoryVulcanHostRoot").Value!;
        var start = new ProcessStartInfo(Path.Combine(hostRoot, "HistoryVulcan.Cli.exe"))
        {
            RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false, StandardOutputEncoding = Encoding.UTF8,
        };
        foreach (var argument in new[] { "--probe", archive, "--format", "json", "--cli", "echo.app.stop" })
            start.ArgumentList.Add(argument);
        using var process = Process.Start(start)!;
        var output = await process.StandardOutput.ReadToEndAsync();
        await process.WaitForExitAsync();
        True(output.TrimStart().StartsWith('{'), $"--probe 没有输出 JSON（退出码 {process.ExitCode}）：{output}");
        using var probe = JsonDocument.Parse(output);
        var module = probe.RootElement.GetProperty("data").GetProperty("module");
        True(module.GetProperty("attached").GetBoolean(), "宿主没有接上模块：" + output);
        Equal(8, module.GetProperty("commandCount").GetInt32());
        True(probe.RootElement.GetProperty("data").GetProperty("result").GetProperty("success").GetBoolean(), output);
    }
    finally
    {
        Directory.Delete(root, recursive: true);
    }
}

static void True(bool condition, string message)
{
    if (!condition)
        throw new InvalidOperationException(message);
}

static void Equal<T>(T expected, T actual)
{
    if (!EqualityComparer<T>.Default.Equals(expected, actual))
        throw new InvalidOperationException($"期望 {expected}，实际 {actual}");
}

static void Throws<T>(Action action) where T : Exception
{
    try
    {
        action();
    }
    catch (T)
    {
        return;
    }

    throw new InvalidOperationException($"期望抛出 {typeof(T).Name}");
}

static async Task<T> ThrowsAsync<T>(Func<Task> action) where T : Exception
{
    try
    {
        await action();
    }
    catch (T ex)
    {
        return ex;
    }

    throw new InvalidOperationException($"期望抛出 {typeof(T).Name}");
}

/// <summary>在空闲端口上起一份真的 HttpHost + EchoApp（假推理、临时库）。</summary>
internal sealed class TestServer : IAsyncDisposable
{
    private readonly HttpHost _host;

    private TestServer(Workspace workspace, EchoApp app, HttpHost host, FakeInference fake)
    {
        Workspace = workspace;
        App = app;
        _host = host;
        Fake = fake;
    }

    public Workspace Workspace { get; }

    public EchoApp App { get; }

    public FakeInference Fake { get; }

    public HttpClient Client { get; } = new() { Timeout = TimeSpan.FromSeconds(30) };

    public string Url => _host.Url;

    public static Task<TestServer> StartAsync(FakeInference fake)
    {
        var workspace = new Workspace();
        var app = new EchoApp(workspace.Paths, fake, _ => { });
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        var host = new HttpHost(port, false, null, app.HandleAsync, _ => { });
        host.Start();
        return Task.FromResult(new TestServer(workspace, app, host, fake));
    }

    public async Task<(int Status, string Body)> SendAsync(HttpMethod method, string path, string? json = null)
    {
        using var request = new HttpRequestMessage(method, Url + path);
        if (json is not null)
            request.Content = new StringContent(json, Encoding.UTF8, "application/json");
        using var response = await Client.SendAsync(request);
        return ((int)response.StatusCode, await response.Content.ReadAsStringAsync());
    }

    public async Task<JsonObject> GetJsonAsync(string path)
    {
        var (status, body) = await SendAsync(HttpMethod.Get, path);
        if (status != 200)
            throw new InvalidOperationException($"GET {path} → {status}: {body}");
        return JsonNode.Parse(body)!.AsObject();
    }

    public async Task<JsonArray> GetArrayAsync(string path)
        => JsonNode.Parse((await SendAsync(HttpMethod.Get, path)).Body)!.AsArray();

    public ValueTask DisposeAsync()
    {
        _host.Dispose();
        App.Dispose();
        Client.Dispose();
        Workspace.Dispose();
        return ValueTask.CompletedTask;
    }
}
