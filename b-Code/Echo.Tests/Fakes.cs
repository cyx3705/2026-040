using System.IO.Compression;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json.Nodes;
using Echo.Inference;
using Echo.Learning;
using HistoryVulcan.Core.Commands;

namespace Echo.Tests;

/// <summary>不碰推理服务的假推理：记下调用，按脚本回答。</summary>
internal sealed class FakeInference : IInference
{
    public bool Running { get; set; } = true;

    public bool Shared { get; set; }

    public string Current { get; set; } = "qwen3:8b";

    public List<string> Calls { get; } = [];

    public IReadOnlyList<TokenCandidate> Tops { get; set; } = [];

    public string[] Reply { get; set; } = ["Hello", " there", "!"];

    public Func<string, string> Answer { get; set; } = _ => "{}";

    public Func<IReadOnlyList<string>, string> Label { get; set; } = labels => labels[^1];

    public int OpenSequences { get; private set; }

    private void Call(string name)
    {
        lock (Calls)
            Calls.Add(name);
    }

    public Task<JsonObject> StatusAsync(CancellationToken cancellation)
    {
        Call("status");
        return Task.FromResult(new JsonObject
        {
            ["running"] = Running, ["current"] = Running ? Current : null,
            ["models"] = new JsonArray(
                new JsonObject { ["name"] = "qwen3:8b", ["family"] = "qwen", ["size"] = 5, ["context"] = 32768, ["installed"] = true, ["supported"] = true, ["reason"] = "" },
                new JsonObject { ["name"] = "gemma4:12b", ["family"] = "gemma4", ["size"] = 7, ["context"] = 16384, ["installed"] = true, ["supported"] = true, ["reason"] = "" }),
        });
    }

    public Task SwitchModelAsync(string name, CancellationToken cancellation)
    {
        Call("use " + name);
        Current = name;
        return Task.CompletedTask;
    }

    public Task<IReadOnlyList<TokenCandidate>> TopTokensAsync(string prompt, int top, string model, CancellationToken cancellation)
    {
        Call("probs");
        return Task.FromResult(Tops);
    }

    public Task<string> AskAsync(string model, string prompt, int maxTokens, JsonObject? schema, double temperature, CancellationToken cancellation)
    {
        Call("ask");
        return Task.FromResult(Answer(prompt));
    }

    public async IAsyncEnumerable<string> ChatAsync(string model, string? system, IReadOnlyList<ChatTurn> messages, int maxTokens,
        [EnumeratorCancellation] CancellationToken cancellation)
    {
        Call("chat");
        foreach (var piece in Reply)
        {
            await Task.Yield();
            yield return piece;
        }
    }

    public Task<KvCapabilities> CapabilitiesAsync(string model, CancellationToken cancellation)
    {
        Call("caps");
        return Task.FromResult(new KvCapabilities(Shared, "fake", Shared ? null : "fake_unsupported", new JsonObject { ["supported"] = Shared }));
    }

    public Task<SequenceHandle> OpenSequenceAsync(string model, string text, CancellationToken cancellation)
    {
        Call("open");
        OpenSequences++;
        return Task.FromResult(new SequenceHandle("1", "e", text.Length));
    }

    public Task<ChooseResult> ChooseAsync(SequenceHandle sequence, string prefixText, string suffixText, IReadOnlyList<string> labels, CancellationToken cancellation)
    {
        Call("choose");
        return Task.FromResult(new ChooseResult(Label(labels), 10, 0));
    }

    public async IAsyncEnumerable<string> SequenceChatAsync(SequenceHandle sequence, int maxTokens, [EnumeratorCancellation] CancellationToken cancellation)
    {
        Call("seqstream");
        foreach (var piece in Reply)
        {
            await Task.Yield();
            yield return piece;
        }
    }

    public Task CloseSequenceAsync(SequenceHandle sequence)
    {
        Call("close");
        OpenSequences--;
        return Task.CompletedTask;
    }
}

/// <summary>一次性目录：迷你词典库 + 用户库。</summary>
internal sealed class Workspace : IDisposable
{
    public Workspace()
    {
        Root = Path.Combine(Path.GetTempPath(), "Echo.Tests", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path.Combine(Root, "data", "dict"));
        var csv = Path.Combine(Root, "ecdict.csv");
        File.WriteAllText(csv, """
            word,phonetic,definition,translation,pos,collins,oxford,tag,bnc,frq,exchange,detail,audio
            go,gəʊ,,"v. 去, 走\nn. 尝试",,,,,,50,p:went/d:gone,,
            went,went,,"v. 去(go的过去式)",,,,,,300,0:go/1:p,,
            bank,bæŋk,,"n. 银行, 河岸\nv. 存款",,,,,,400,,,
            sweet,swiːt,,"n. 糖果\na. 甜的",,,,,,500,,,
            make,meɪk,,"v. 做, 制造",,,,,,60,,,
            making,,,"v. 制作",,,,,,900,0:make/1:i,,
            apple,ˈæpl,,"n. 苹果",,,,,,700,,,
            applejuice,,,"n. 苹果汁",,,,,,800,,,
            river,,,"n. 河, 江",,,,,,650,,,
            """.Replace("\\n", "\\n"), new UTF8Encoding(false));
        Echo.Learning.Dictionary.Build(csv, Path.Combine(Root, "data", "dict", "ecdict.db"));
        Paths = new EchoPaths(Path.Combine(Root, "data"), Path.Combine(Root, "user", "user.db"));
    }

    public string Root { get; }

    public EchoPaths Paths { get; }

    public void Dispose()
    {
        try
        {
            Directory.Delete(Root, recursive: true);
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }
}

/// <summary>造一个最小 .docx（只有 word/document.xml）。</summary>
internal static class Docx
{
    public static byte[] Make(string bodyXml)
    {
        using var buffer = new MemoryStream();
        using (var zip = new ZipArchive(buffer, ZipArchiveMode.Create, leaveOpen: true))
        {
            var entry = zip.CreateEntry("word/document.xml");
            using var writer = new StreamWriter(entry.Open(), new UTF8Encoding(false));
            writer.Write("<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"><w:body>"
                         + bodyXml + "</w:body></w:document>");
        }

        return buffer.ToArray();
    }

    public static string P(string text) => $"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>";
}

/// <summary>登记口替身。</summary>
internal sealed class Registrar : ICommandRegistrar
{
    private readonly Dictionary<string, CommandDescriptor> _commands = new(StringComparer.OrdinalIgnoreCase);

    public IReadOnlyCollection<CommandDescriptor> All => _commands.Values;

    public void Register(CommandDescriptor descriptor)
    {
        if (!_commands.TryAdd(descriptor.Name, descriptor))
            throw new InvalidOperationException($"重复登记 {descriptor.Name}");
    }

    public bool TryGet(string name, out CommandDescriptor? descriptor) => _commands.TryGetValue(name, out descriptor);
}

/// <summary>总线替身：按指令名给回执，记下指令全文。</summary>
internal sealed class FakeBus(Func<string, CommandResult> respond) : ICommandBus
{
    public List<string> Commands { get; } = [];

    public Task<CommandResult> ExecuteAsync(string text, string source, CancellationToken cancellation = default) => InvokeAsync(text, source, cancellation);

    public Task<CommandResult> InvokeAsync(string text, string source, CancellationToken cancellation = default)
    {
        lock (Commands)
            Commands.Add(text);
        return Task.FromResult(respond(text));
    }

    public bool RequestConfirmation(string prompt) => false;
}
