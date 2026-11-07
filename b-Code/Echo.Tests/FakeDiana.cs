using System.Text.Json;
using System.Text.Json.Nodes;
using Echo.Data;
using HistoryVulcan.Core.Commands;

namespace Echo.Tests;

/// <summary>
/// 假的源码区 Diana：只答 diana.z.query / diana.z.exec，背后是临时目录里的本地 SQLite。
/// 回执形状与 HistoryDiana 3.1.0 一致（数据经 JSON 往返），用来验证 Echo 走 z 地址这条路。
/// </summary>
internal sealed class FakeDiana(string address, string file) : ICommandBus, IDisposable
{
    private readonly IUserDb _db = new LocalUserDb(file);

    public List<string> Calls { get; } = [];

    public Task<CommandResult> ExecuteAsync(string text, string source, CancellationToken cancellation = default)
        => InvokeAsync(text, source, cancellation);

    public Task<CommandResult> InvokeAsync(string text, string source, CancellationToken cancellation = default)
    {
        var parsed = CommandParser.Parse(text);
        Calls.Add(parsed.Name);
        var named = parsed.Named;
        if (named["path"] != address)
            return Task.FromResult(CommandResult.Fail("地址不对：" + named["path"]));
        try
        {
            lock (_db.Gate)
            {
                if (parsed.Name == "diana.z.query")
                {
                    var rows = _db.Query(named["sql"], Args(named.GetValueOrDefault("args"), []));
                    return Ok(new { columns = Array.Empty<string>(), rows = rows.Select(Row).ToList(), count = rows.Count, truncated = false });
                }

                if (parsed.Name == "diana.z.exec")
                {
                    var steps = JsonNode.Parse(named["steps"])!.AsArray();
                    var rowIds = new List<long>();
                    var results = new List<object>();
                    _db.Script("BEGIN;");
                    try
                    {
                        foreach (var step in steps)
                        {
                            // 真 Diana 无参数时可执行多条语句（建表脚本），这里同样处理。
                            var sql = step!["sql"]!.GetValue<string>();
                            var args = Args(step["args"]?.ToJsonString(), rowIds);
                            var changes = 0;
                            if (args.Length == 0)
                                _db.Script(sql);
                            else
                                changes = _db.Execute(sql, args);
                            var last = _db.QueryOne("SELECT last_insert_rowid()")![0] as long? ?? 0;
                            rowIds.Add(last);
                            results.Add(new { changes, lastRowId = last });
                        }

                        _db.Script("COMMIT;");
                    }
                    catch
                    {
                        _db.Script("ROLLBACK;");
                        throw;
                    }

                    return Ok(new { steps = results });
                }
            }
        }
        catch (Exception ex)
        {
            return Task.FromResult(CommandResult.Fail(ex.Message));
        }

        return Task.FromResult(CommandResult.Fail("未知指令 " + parsed.Name));
    }

    public bool RequestConfirmation(string prompt) => false;

    public void Dispose() => _db.Dispose();

    // 和真 Diana 一样，数据经 JSON 往返一次：整值的小数会变成整数。
    private static Task<CommandResult> Ok(object data)
        => Task.FromResult(CommandResult.Ok("ok", JsonSerializer.SerializeToElement(data)));

    private static object?[] Row(object?[] row)
        => row.Select(value => value is byte[] blob ? new { base64 = Convert.ToBase64String(blob) } : value).ToArray();

    private static object?[] Args(string? json, IReadOnlyList<long> rowIds)
    {
        if (string.IsNullOrWhiteSpace(json))
            return [];
        return JsonNode.Parse(json)!.AsArray().Select(node => node switch
        {
            null => null,
            JsonObject o when o["rowid"] is { } r => (object?)rowIds[r.GetValue<int>()],
            JsonObject o when o["base64"] is { } b => Convert.FromBase64String(b.GetValue<string>()),
            JsonValue v when v.GetValueKind() == JsonValueKind.String => v.GetValue<string>(),
            JsonValue v when v.ToJsonString().Contains('.') => v.GetValue<double>(),
            JsonValue v => v.GetValue<long>(),
            _ => throw new InvalidOperationException("参数不对"),
        }).ToArray();
    }
}
