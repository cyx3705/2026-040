using System.Globalization;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using Echo.Learning;
using HistoryVulcan.Core.Commands;

namespace Echo.Inference;

/// <summary>推理调用失败（Ratio 回执的失败原因）。</summary>
internal class InferenceException(string message) : Exception(message);

/// <summary>共享 KV 被服务拒绝或不支持；<see cref="Exception.Message"/> 是服务的原因码（如 invalid_sequence_size）。</summary>
internal sealed class KvUnsupportedException(string message) : InferenceException(message);

/// <summary>共享 KV 能力。</summary>
internal sealed record KvCapabilities(bool Supported, string Backend, string? Reason, JsonObject Raw);

/// <summary>共享 KV 序列句柄。</summary>
internal sealed record SequenceHandle(string Id, string Epoch, int PrefixTokens);

/// <summary>一次分叉选标签的结果。</summary>
internal sealed record ChooseResult(string Label, int SharedPrefixTokens, int ReplayedTokens);

/// <summary>下一 token 候选。</summary>
internal sealed record TokenCandidate(string Token, double Logprob);

/// <summary>
/// Echo 需要的全部推理能力。实现只有一个：<see cref="RatioInference"/>，经宿主总线调 Ratio 的指令；
/// 测试换成假的。Echo 不直连任何推理服务。
/// </summary>
internal interface IInference
{
    /// <summary>本机推理服务状态（ratio.local.status 的 Data）。</summary>
    Task<JsonObject> StatusAsync(CancellationToken cancellation);

    /// <summary>换模并等到就绪（ratio.local.use）。</summary>
    Task SwitchModelAsync(string name, CancellationToken cancellation);

    Task<IReadOnlyList<TokenCandidate>> TopTokensAsync(string prompt, int top, string model, CancellationToken cancellation);

    /// <summary>一次非流式对话；<paramref name="schema"/> 为 JSON Schema 时按它严格约束输出。</summary>
    Task<string> AskAsync(string model, string prompt, int maxTokens, JsonObject? schema, double temperature, CancellationToken cancellation);

    IAsyncEnumerable<string> ChatAsync(string model, string? system, IReadOnlyList<ChatTurn> messages, int maxTokens, CancellationToken cancellation);

    Task<KvCapabilities> CapabilitiesAsync(string model, CancellationToken cancellation);

    Task<SequenceHandle> OpenSequenceAsync(string model, string text, CancellationToken cancellation);

    Task<ChooseResult> ChooseAsync(SequenceHandle sequence, string prefixText, string suffixText, IReadOnlyList<string> labels, CancellationToken cancellation);

    IAsyncEnumerable<string> SequenceChatAsync(SequenceHandle sequence, int maxTokens, CancellationToken cancellation);

    Task CloseSequenceAsync(SequenceHandle sequence);
}

/// <summary>
/// 经宿主总线调 Ratio 的推理指令。一律走安静通道 <c>InvokeAsync</c>：逐词预测、逐词选义一轮几十次，进控制台只会刷屏。
/// 本机模型都走 <c>provider=local</c>，对话一律 <c>think=false</c>（逐词交互不要思考块）。
/// </summary>
internal sealed class RatioInference(ICommandBus bus) : IInference
{
    private const string Provider = "local";

    /// <summary>流式生成多久没有新正文就判为卡住。</summary>
    internal static TimeSpan StallLimit { get; set; } = TimeSpan.FromMinutes(2);

    private static string Q(string value) => CommandParser.QuoteArg(value);

    private static string Q(JsonNode node) => CommandParser.QuoteArg(node.ToJsonString(Json.Options));

    private async Task<JsonObject> CallAsync(string command, CancellationToken cancellation)
    {
        var result = await bus.InvokeAsync(command, "", cancellation).ConfigureAwait(false);
        if (!result.Success)
        {
            var message = result.Message ?? "推理指令失败";
            const string kv = "shared_kv:";
            var at = message.IndexOf(kv, StringComparison.Ordinal);
            if (at >= 0)
                throw new KvUnsupportedException(message[(at + kv.Length)..].Trim());
            throw new InferenceException(message);
        }

        return DataOf(result) as JsonObject ?? throw new InferenceException($"{command.Split(' ')[0]} 回执没有 JSON 对象");
    }

    /// <summary>回执 Data：模块指令给的是 JSON 文本，宿主指令给的是 JsonElement。</summary>
    internal static JsonNode? DataOf(CommandResult result) => result.Data switch
    {
        null => null,
        string text => JsonNode.Parse(text),
        JsonElement element => JsonNode.Parse(element.GetRawText()),
        JsonNode node => node,
        var other => JsonSerializer.SerializeToNode(other),
    };

    public Task<JsonObject> StatusAsync(CancellationToken cancellation) => CallAsync("ratio.local.status", cancellation);

    public async Task SwitchModelAsync(string name, CancellationToken cancellation)
        => await CallAsync($"ratio.local.use name={Q(name)} timeout=900", cancellation).ConfigureAwait(false);

    public async Task<IReadOnlyList<TokenCandidate>> TopTokensAsync(string prompt, int top, string model, CancellationToken cancellation)
    {
        var data = await CallAsync($"ratio.local.probs prompt={Q(prompt)} top={top} model={Q(model)} timeout=45", cancellation).ConfigureAwait(false);
        return (data["tokens"] as JsonArray ?? [])
            .OfType<JsonObject>()
            .Select(t => new TokenCandidate(t["token"]?.GetValue<string>() ?? "", t["logprob"]?.GetValue<double>() ?? double.NegativeInfinity))
            .ToList();
    }

    public async Task<string> AskAsync(string model, string prompt, int maxTokens, JsonObject? schema, double temperature, CancellationToken cancellation)
    {
        var messages = new JsonArray(new JsonObject { ["role"] = "user", ["content"] = prompt });
        var command = $"ratio.chat.send messages={Q(messages)} provider={Provider} model={Q(model)} maxtokens={maxTokens} "
                      + $"temperature={temperature.ToString(CultureInfo.InvariantCulture)} think=false timeout=120";
        if (schema is not null)
            command += " schema=" + Q(schema);
        var data = await CallAsync(command, cancellation).ConfigureAwait(false);
        if (data["finishReason"]?.GetValue<string>() == "length" || data["finish_reason"]?.GetValue<string>() == "length")
            throw new InferenceException("推理结果超过输出预算");
        return data["content"]?.GetValue<string>() ?? "";
    }

    public async IAsyncEnumerable<string> ChatAsync(string model, string? system, IReadOnlyList<ChatTurn> messages, int maxTokens,
        [EnumeratorCancellation] CancellationToken cancellation)
    {
        var array = new JsonArray();
        if (!string.IsNullOrEmpty(system))
            array.Add(new JsonObject { ["role"] = "system", ["content"] = system });
        foreach (var m in messages)
            array.Add(new JsonObject { ["role"] = m.Role, ["content"] = m.Content });
        if (array.Count == 0)
            throw new ArgumentException("对话请求缺少 messages");
        var opened = await CallAsync($"ratio.chat.stream messages={Q(array)} provider={Provider} model={Q(model)} maxtokens={maxTokens} think=false",
            cancellation).ConfigureAwait(false);
        await foreach (var delta in ReadStreamAsync(opened, cancellation).ConfigureAwait(false))
            yield return delta;
    }

    public async Task<KvCapabilities> CapabilitiesAsync(string model, CancellationToken cancellation)
    {
        try
        {
            var data = await CallAsync($"ratio.seq.caps model={Q(model)}", cancellation).ConfigureAwait(false);
            return new KvCapabilities(data["supported"]?.GetValueKind() == JsonValueKind.True,
                data["backend"]?.ToString() ?? "external-shared-kv", data["reason"]?.ToString(), data);
        }
        catch (InferenceException ex)
        {
            return new KvCapabilities(false, "ratio", ex.Message, new JsonObject { ["supported"] = false, ["reason"] = ex.Message });
        }
    }

    public async Task<SequenceHandle> OpenSequenceAsync(string model, string text, CancellationToken cancellation)
    {
        var data = await CallAsync($"ratio.seq.open text={Q(text)} model={Q(model)}", cancellation).ConfigureAwait(false);
        return new SequenceHandle(data["sequence"]!.ToString(), data["epoch"]!.ToString(), data["prefixTokens"]!.GetValue<int>());
    }

    public async Task<ChooseResult> ChooseAsync(SequenceHandle sequence, string prefixText, string suffixText, IReadOnlyList<string> labels,
        CancellationToken cancellation)
    {
        var data = await CallAsync(
            $"ratio.seq.choose sequence={Q(sequence.Id)} epoch={Q(sequence.Epoch)} prefix={Q(prefixText)} suffix={Q(suffixText)} "
            + $"labels={Q(new JsonArray(labels.Select(l => (JsonNode)JsonValue.Create(l)!).ToArray()))}", cancellation).ConfigureAwait(false);
        return new ChooseResult(data["label"]?.GetValue<string>() ?? "",
            data["shared_prefix_tokens"]?.GetValue<int>() ?? 0, data["replayed_tokens"]?.GetValue<int>() ?? 0);
    }

    public async IAsyncEnumerable<string> SequenceChatAsync(SequenceHandle sequence, int maxTokens, [EnumeratorCancellation] CancellationToken cancellation)
    {
        var opened = await CallAsync($"ratio.seq.stream sequence={Q(sequence.Id)} epoch={Q(sequence.Epoch)} maxtokens={maxTokens}", cancellation)
            .ConfigureAwait(false);
        await foreach (var delta in ReadStreamAsync(opened, cancellation, kv: true).ConfigureAwait(false))
            yield return delta;
    }

    public async Task CloseSequenceAsync(SequenceHandle sequence)
        => await CallAsync($"ratio.seq.close sequence={Q(sequence.Id)} epoch={Q(sequence.Epoch)}", CancellationToken.None).ConfigureAwait(false);

    private async IAsyncEnumerable<string> ReadStreamAsync(JsonObject opened, [EnumeratorCancellation] CancellationToken cancellation, bool kv = false)
    {
        var id = opened["stream"]?.GetValue<string>() ?? throw new InferenceException("开流回执没有 stream 编号");
        var after = 0;
        var finished = false;
        var progressed = DateTime.UtcNow;
        try
        {
            while (true)
            {
                // 服务卡住时不无限等：两分钟没有新正文就放弃（与旧版 120 秒读超时一致），流随即取消。
                if (DateTime.UtcNow - progressed > StallLimit)
                    throw kv ? new KvUnsupportedException("stream_stalled") : new InferenceException("推理服务两分钟没有新输出，已放弃这一轮");
                var chunk = await CallAsync($"ratio.stream.read stream={id} after={after} wait=5000", cancellation).ConfigureAwait(false);
                var next = chunk["next"]?.GetValue<int>() ?? after;
                if (next != after)
                    progressed = DateTime.UtcNow;
                after = next;
                var text = chunk["text"]?.GetValue<string>();
                if (!string.IsNullOrEmpty(text))
                    yield return text;
                if (chunk["done"]?.GetValueKind() == JsonValueKind.True)
                {
                    finished = true;
                    if (chunk["error"]?.GetValue<string>() is { Length: > 0 } error)
                        throw kv ? new KvUnsupportedException(error) : new InferenceException(error);
                    yield break;
                }
            }
        }
        finally
        {
            if (!finished)
                _ = bus.InvokeAsync($"ratio.stream.cancel stream={id}", "", CancellationToken.None);
        }
    }
}
