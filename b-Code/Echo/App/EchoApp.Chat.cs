using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Echo.Inference;
using Echo.Learning;
using Echo.Web;

namespace Echo.App;

internal sealed partial class EchoApp
{
    /// <summary>
    /// 流式回复；说完才存档（没成的一轮不进历史），done 事件带回对话编号。练词对话可以没有消息就开始：AI 先开口。
    /// 共享 KV 可用时 A 在服务里持续生成，每个说完的词在 A 上分叉 B 选义，与 A 并行；否则说完再逐词选义。
    /// </summary>
    private async Task ChatAsync(EchoRequest request)
    {
        var body = await request.JsonAsync().ConfigureAwait(false);
        var messages = Turns(body["messages"]);
        var level = body["level"]?.GetValue<string>();
        var length = Int(body, "length", 0);
        long? conversationId = body["conversation_id"] is JsonValue v && v.TryGetValue<long>(out var id) ? id : null;
        var plan = body["practice"] is JsonObject p && Practice.WordsOf(p).Count > 0 ? p : null;
        var prompt = messages.Count > 0 ? messages : plan is not null ? [new ChatTurn("user", Practice.Opening)] : [];
        var sse = new SseWriter(request.Response);
        var generation = Guid.NewGuid().ToString("N");
        using var aborted = CancellationTokenSource.CreateLinkedTokenSource(request.Aborted);
        var token = aborted.Token;

        async Task Send(JsonObject payload)
        {
            payload["generation"] = generation;
            if (!await sse.SendAsync(payload).ConfigureAwait(false))
                aborted.Cancel();
        }

        using (await Gate.UseAsync(token).ConfigureAwait(false))
        {
            await Gate.Slot.WaitAsync(token).ConfigureAwait(false);
            try
            {
                var reply = new System.Text.StringBuilder();
                var glosses = new List<JsonObject>();
                try
                {
                    var system = SystemFor(level, length, plan, messages);
                    await Send(new JsonObject()).ConfigureAwait(false);
                    var model = CurrentModel;
                    var template = await TemplateAsync(model, token).ConfigureAwait(false);
                    Log($"TRACE chat template {template}");
                    var shared = template is not null && (await Inference.CapabilitiesAsync(model, token).ConfigureAwait(false)).Supported;
                    Log($"TRACE chat shared {shared}");
                    if (shared)
                    {
                        try
                        {
                            await SharedChatAsync(model, template!, system, prompt, messages, reply, glosses, Send, token).ConfigureAwait(false);
                        }
                        catch (KvUnsupportedException ex) when (reply.Length == 0 && ex.Message == "invalid_sequence_size")
                        {
                            // A 的诊断租约预算比服务普通对话上下文小：长对话退回普通对话。
                            shared = false;
                        }
                    }

                    if (!shared)
                    {
                        using (await Gate.RequestAsync(token).ConfigureAwait(false))
                        {
                            await foreach (var text in Inference.ChatAsync(model, system, prompt, 1024, token).ConfigureAwait(false))
                            {
                                reply.Append(text);
                                await Send(new JsonObject { ["delta"] = text }).ConfigureAwait(false);
                            }
                        }

                        await Send(new JsonObject { ["text_done"] = true }).ConfigureAwait(false);
                        glosses = await ResolveGlossesAsync(reply.ToString(), messages, false, null, null, token).ConfigureAwait(false);
                    }

                    foreach (var item in glosses)
                        item["generation"] = generation;
                    // B 乱序完成：存档和最终事件按原文顺序。
                    glosses = glosses.OrderBy(i => i["start"]?.GetValue<int>() ?? 0).ThenBy(i => i["end"]?.GetValue<int>() ?? 0).ToList();
                    var all = new JsonArray(glosses.Select(g => (JsonNode)g.DeepClone()).ToArray());
                    await Send(new JsonObject { ["gloss"] = all.DeepClone() }).ConfigureAwait(false);
                    var history = new List<ChatTurn>(messages) { new("assistant", reply.ToString(), (JsonArray)all.DeepClone()) };
                    var conv = Store.Save(conversationId, history, Levels.Normalize(level), plan,
                        plan is not null ? $"练词 · {(plan["label"]?.GetValue<string>() is { Length: > 0 } label ? label : "单词表")}" : null);
                    var used = Practice.UsedWords(plan, messages, Dictionary.Lookup).Order(StringComparer.Ordinal).ToList();
                    if (plan?["list_id"] is JsonValue listValue && listValue.TryGetValue<long>(out var listId) && messages.Count > 0)
                    {
                        var before = Practice.UsedWords(plan, messages.Take(messages.Count - 1), Dictionary.Lookup);
                        Store.CountUsed(listId, used.Where(w => !before.Contains(w)));
                    }

                    await Send(new JsonObject { ["gloss_done"] = true }).ConfigureAwait(false);
                    await Send(new JsonObject
                    {
                        ["done"] = true, ["conversation_id"] = conv,
                        ["used"] = new JsonArray(used.Select(u => (JsonNode)JsonValue.Create(u)!).ToArray()), ["glosses"] = all,
                    }).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                }
                catch (Exception ex)
                {
                    Log($"对话失败：{ex.GetType().Name}: {ex.Message}");
                    await Send(new JsonObject { ["error"] = ex.Message }).ConfigureAwait(false);
                }
            }
            finally
            {
                Gate.Slot.Release();
            }
        }
    }

    private async Task SharedChatAsync(string model, string template, string system, IReadOnlyList<ChatTurn> prompt, IReadOnlyList<ChatTurn> messages,
        System.Text.StringBuilder reply, List<JsonObject> glosses, Func<JsonObject, Task> send, CancellationToken token)
    {
        var sourcePrompt = Prompts.AssistantPrompt(template, system, prompt);
        using var lease = await Gate.RequestAsync(token).ConfigureAwait(false);
        Log("TRACE chat lease");
        var sequence = await Inference.OpenSequenceAsync(model, sourcePrompt, token).ConfigureAwait(false);
        Log($"TRACE chat open {sequence.Id}");
        var context = new SharedContext(sequence, sourcePrompt, template);
        var processed = new HashSet<int>();
        var remaining = 8;
        var pending = new List<Task<List<JsonObject>>>();
        try
        {
            void Schedule(bool final)
            {
                var text = reply.ToString();
                foreach (Match match in WordsRx().Matches(text))
                {
                    var end = match.Index + match.Length;
                    if ((end >= text.Length && !final) || !processed.Add(end))
                        continue;
                    var word = match.Value;
                    var entry = Dictionary.Lookup(word);
                    var multi = entry is not null && !Dictionary.LearnedLemmas().Contains(entry.Lemma) && Dictionary.Senses(word).Count > 1;
                    var defaultOnly = remaining <= 0;
                    if (multi && !defaultOnly)
                        remaining--;
                    var prefix = text[..end];
                    var start = match.Index;
                    pending.Add(Task.Run(async () =>
                    {
                        var items = await ResolveGlossesAsync(word, messages, defaultOnly, context, prefix, token).ConfigureAwait(false);
                        foreach (var item in items)
                        {
                            item["start"] = start;
                            item["end"] = end;
                        }

                        return items;
                    }, token));
                }
            }

            async Task Drain(bool wait)
            {
                while (pending.Count > 0)
                {
                    var done = pending.Where(t => t.IsCompleted).ToList();
                    if (done.Count == 0)
                    {
                        if (!wait)
                            return;
                        await Task.WhenAny(pending).ConfigureAwait(false);
                        continue;
                    }

                    foreach (var task in done)
                    {
                        pending.Remove(task);
                        foreach (var item in await task.ConfigureAwait(false))
                        {
                            glosses.Add(item);
                            await send(new JsonObject { ["gloss"] = new JsonArray(item.DeepClone()) }).ConfigureAwait(false);
                        }
                    }

                    if (!wait)
                        return;
                }
            }

            await foreach (var text in Inference.SequenceChatAsync(sequence, 1024, token).ConfigureAwait(false))
            {
                reply.Append(text);
                Log($"TRACE chat delta {reply.Length}");
                await send(new JsonObject { ["delta"] = text }).ConfigureAwait(false);
                Schedule(false);
                await Drain(false).ConfigureAwait(false);
            }

            Schedule(true);
            await Drain(false).ConfigureAwait(false);
            await send(new JsonObject { ["text_done"] = true }).ConfigureAwait(false);
            await Drain(true).ConfigureAwait(false);
        }
        finally
        {
            if (pending.Count > 0)
            {
                try
                {
                    await Task.WhenAll(pending).ConfigureAwait(false);
                }
                catch (Exception)
                {
                }
            }

            try
            {
                await Inference.CloseSequenceAsync(sequence).ConfigureAwait(false);
            }
            catch (InferenceException ex)
            {
                Log($"释放序列失败：{ex.Message}");
            }
        }
    }
}
