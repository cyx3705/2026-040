using System.Text.Json;
using System.Text.Json.Nodes;
using Echo.Inference;
using Echo.Learning;
using Echo.Web;

namespace Echo.App;

internal sealed partial class EchoApp
{
    /// <summary>模型族：输入预测每轮都要用，记在本地，换模时清空。</summary>
    internal async Task<string> FamilyAsync(string model, CancellationToken cancellation)
    {
        lock (_familyGate)
        {
            if (_families.TryGetValue(model, out var known))
                return known;
        }

        try
        {
            var status = await Inference.StatusAsync(cancellation).ConfigureAwait(false);
            lock (_familyGate)
            {
                foreach (var m in (status["models"] as JsonArray ?? []).OfType<JsonObject>())
                {
                    if (m["name"]?.GetValue<string>() is { } name)
                        _families[name] = m["family"]?.GetValue<string>() ?? "";
                }

                if (!_families.ContainsKey(model))
                    _families[model] = model.Contains("qwen", StringComparison.OrdinalIgnoreCase) ? "qwen"
                        : model.Contains("gemma4", StringComparison.OrdinalIgnoreCase) ? "gemma4"
                        : model.Contains("gemma", StringComparison.OrdinalIgnoreCase) ? "gemma" : "";
                return _families[model];
            }
        }
        catch (InferenceException)
        {
            return "";
        }
    }

    internal async Task<string?> TemplateAsync(string model, CancellationToken cancellation)
        => Prompts.TemplateOf(await FamilyAsync(model, cancellation).ConfigureAwait(false));

    private JsonObject DebugSettings() => new()
    {
        ["candidates"] = int.TryParse(Store.Setting("suggest_candidates", "5"), out var c) ? c : 5,
        ["parallel"] = Gate.Parallel,
        ["max_candidates"] = Prompts.TopN,
    };

    /// <summary>页面与扩展轮询的状态。服务只装一个模型：在跑时 Echo 的当前模型跟着服务走。</summary>
    internal async Task<JsonObject> StatusAsync(CancellationToken cancellation)
    {
        JsonObject service;
        try
        {
            service = await Inference.StatusAsync(cancellation).ConfigureAwait(false);
        }
        catch (InferenceException ex)
        {
            return Base("stopped", "无法通过 Ratio 取得本机推理服务状态：" + ex.Message);
        }

        if (service["switch"] is JsonObject job && job["state"]?.GetValue<string>() == "switching")
        {
            return new JsonObject
            {
                ["state"] = "switching", ["model"] = CurrentModel, ["detail"] = "统一服务正在更换模型",
                ["model_switch"] = job.DeepClone(), ["dictionary"] = Dictionary.Installed,
            };
        }

        if (service["running"]?.GetValueKind() != JsonValueKind.True)
            return Base("stopped", "本机推理服务没在运行（可在控制台执行 ratio.local.start）");
        var current = service["current"]?.GetValue<string>();
        if (string.IsNullOrEmpty(current))
            return Base("no-model", "本机推理服务没有装载模型");
        if (current != CurrentModel)
            Store.SetSetting("model", current);

        var result = Base("ready", "外部推理服务就绪");
        result["model"] = current;
        var kv = await Inference.CapabilitiesAsync(current, cancellation).ConfigureAwait(false);
        var raw = kv.Raw;
        var status = new JsonObject
        {
            ["backend"] = kv.Backend, ["supported"] = kv.Supported,
            ["shared_prefix"] = raw["shared_prefix"]?.GetValueKind() == JsonValueKind.True,
            ["fork_at"] = raw["fork_at"]?.GetValueKind() == JsonValueKind.True,
            ["label_constraint"] = raw["label_constraint"]?.GetValueKind() == JsonValueKind.True,
            ["release"] = raw["release"]?.GetValueKind() == JsonValueKind.True,
            ["reason"] = kv.Reason,
        };
        if (raw["provider_parallel_slots"] is { } slots)
            status["provider_parallel_slots"] = slots.DeepClone();
        result["kv"] = status;
        return result;

        JsonObject Base(string state, string detail) => new()
        {
            ["state"] = state, ["detail"] = detail, ["model"] = CurrentModel, ["dictionary"] = Dictionary.Installed,
            ["levels"] = Levels.Catalog(), ["lengths"] = Levels.LengthCatalog(), ["debug"] = DebugSettings(),
        };
    }

    private async Task<JsonObject> SetDebugAsync(JsonObject body, CancellationToken cancellation)
    {
        var candidates = body["candidates"]?.GetValueKind() == JsonValueKind.Number ? body["candidates"]!.GetValue<int>() : -1;
        var parallel = body["parallel"]?.GetValueKind() == JsonValueKind.Number ? body["parallel"]!.GetValue<int>() : -1;
        if (candidates < 1 || candidates > Prompts.TopN || parallel is not (1 or 2))
            throw new HttpError(422, $"candidates 须为 1-{Prompts.TopN}，parallel 须为 1 或 2");
        using (await Gate.ExclusiveAsync(cancellation).ConfigureAwait(false))
        {
            Gate.SetParallel(parallel);
            Store.SetSetting("suggest_candidates", candidates.ToString(System.Globalization.CultureInfo.InvariantCulture));
            Store.SetSetting("engine_parallel", parallel.ToString(System.Globalization.CultureInfo.InvariantCulture));
            return DebugSettings();
        }
    }

    /// <summary>可选模型：来自本机推理服务的登记表（ratio.local.status）。</summary>
    internal async Task<JsonObject> ModelsAsync(CancellationToken cancellation)
    {
        JsonObject service;
        try
        {
            service = await Inference.StatusAsync(cancellation).ConfigureAwait(false);
        }
        catch (InferenceException)
        {
            throw new HttpError(503, "无法读取外部服务模型列表");
        }

        if (service["running"]?.GetValueKind() != JsonValueKind.True)
            throw new HttpError(503, "本机推理服务没在运行，读不到模型列表");
        return new JsonObject
        {
            ["managed"] = true,
            ["models"] = (service["models"] as JsonArray ?? []).DeepClone(),
            ["current"] = service["current"]?.GetValue<string>() ?? CurrentModel,
        };
    }

    internal async Task<JsonObject> SetModelAsync(JsonObject body, CancellationToken cancellation)
    {
        var name = body["name"]?.GetValue<string>() ?? throw new HttpError(422, "缺少 name");
        using (await Gate.ExclusiveAsync(cancellation).ConfigureAwait(false))
        {
            var catalog = await ModelsAsync(cancellation).ConfigureAwait(false);
            var target = (catalog["models"] as JsonArray ?? []).OfType<JsonObject>()
                .FirstOrDefault(m => m["name"]?.GetValue<string>() == name) ?? throw new HttpError(404, "外部服务未提供此模型");
            if (target["supported"]?.GetValueKind() != JsonValueKind.True)
                throw new HttpError(422, target["reason"]?.GetValue<string>() is { Length: > 0 } reason ? reason : "此模型当前不可用");
            // 独占期间没有别的推理在跑：伴读常驻的序列与能力缓存都跟着旧模型作废。
            await DropReaderKvAsync().ConfigureAwait(false);
            _readerCaps = null;
            if (catalog["current"]?.GetValue<string>() != name)
            {
                try
                {
                    await Inference.SwitchModelAsync(name, cancellation).ConfigureAwait(false);
                }
                catch (InferenceException ex)
                {
                    throw new HttpError(503, ex.Message);
                }
            }

            lock (_familyGate)
                _families.Clear();
            Store.SetSetting("model", name);
            return new JsonObject { ["current"] = name };
        }
    }
}
