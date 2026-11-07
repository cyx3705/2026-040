using System.Text.Json;
using System.Text.Json.Nodes;
using HistoryVulcan.Core.Commands;

namespace Echo.Data;

/// <summary>事务里的一步：SQL 与参数。参数可放 <see cref="RowRef"/>，表示前面某步插入的行号。</summary>
internal readonly record struct DbStep(string Sql, params object?[] Args);

/// <summary>批量写里引用第 <see cref="Step"/> 步（从 0 起）插入的行号。</summary>
internal sealed record RowRef(int Step);

/// <summary>
/// 用户数据库（user.db）的访问面。Store 与「学会的词」只认它，不关心库在本机还是在源码区机器上。
/// </summary>
/// <remarks>
/// 0.6.0：user.db 在 z-学习数据 里，是长期资产，z 只在源码区机器上、从不迁移；Echo 自己不再打开它，
/// 生产走 <see cref="ZUserDb"/>（经 <c>diana.z.query</c> / <c>diana.z.exec</c> 由 Diana 持连接），
/// 本机路径（<see cref="LocalUserDb"/>）只给测试与排障。
/// 跨调用的事务做不到，所以要原子的多步写一律走 <see cref="Batch"/>。
/// </remarks>
internal interface IUserDb : IDisposable
{
    /// <summary>调用方共用这把锁来串行使用。</summary>
    object Gate { get; }

    /// <summary>给人看的位置：z 地址或本机文件。</summary>
    string Location { get; }

    /// <summary>执行一段可能含多条语句的 SQL（无参数），例如建表。</summary>
    void Script(string sql);

    /// <summary>执行一条写语句，返回受影响行数。</summary>
    int Execute(string sql, params object?[] args);

    /// <summary>查询全部行；每行是按列序的值（long / double / string / byte[] / null）。</summary>
    List<object?[]> Query(string sql, params object?[] args);

    /// <summary>第一行；没有返回 null。</summary>
    object?[]? QueryOne(string sql, params object?[] args) => Query(sql, args).FirstOrDefault();

    /// <summary>在一个事务里执行多步，任一步失败整体回滚；返回每步之后的最后插入行号。</summary>
    IReadOnlyList<long> Batch(IReadOnlyList<DbStep> steps);
}

/// <summary>本机 SQLite 文件。只给测试与排障用（ECHO_USER_DB 写成绝对路径时）。</summary>
internal sealed class LocalUserDb(string path) : IUserDb
{
    private readonly SqliteConnection _con = Open(path);

    public object Gate => _con.Gate;

    public string Location => path;

    public void Script(string sql) => _con.Script(sql);

    public int Execute(string sql, params object?[] args) => _con.Execute(sql, args);

    public List<object?[]> Query(string sql, params object?[] args) => _con.Query(sql, args);

    public IReadOnlyList<long> Batch(IReadOnlyList<DbStep> steps)
    {
        var rowIds = new List<long>();
        _con.Script("BEGIN;");
        try
        {
            foreach (var step in steps)
            {
                _con.Execute(step.Sql, step.Args.Select(arg => arg is RowRef r ? rowIds[r.Step] : arg).ToArray());
                rowIds.Add(_con.LastInsertRowId);
            }

            _con.Script("COMMIT;");
            return rowIds;
        }
        catch
        {
            _con.Script("ROLLBACK;");
            throw;
        }
    }

    public void Dispose() => _con.Dispose();

    private static SqliteConnection Open(string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        return new SqliteConnection(path);
    }
}

/// <summary>
/// 经 Diana 访问 z 里的 user.db：<c>diana.z.query</c> 读、<c>diana.z.exec</c> 写（一个事务）。连接在源码区机器上由 Diana 持有。
/// </summary>
internal sealed class ZUserDb(ICommandBus bus, string address) : IUserDb
{
    public object Gate { get; } = new();

    public string Location => address;

    public void Script(string sql) => Exec([new DbStep(sql)]);

    public int Execute(string sql, params object?[] args)
        => Exec([new DbStep(sql, args)])[0]["changes"]!.GetValue<int>();

    public List<object?[]> Query(string sql, params object?[] args)
    {
        var data = Invoke($"diana.z.query path={Q(address)} sql={Q(sql)} args={Q(Args(args, 0))} limit=100000");
        if (data["truncated"]?.GetValue<bool>() == true)
            throw new SqliteException($"查询结果超过 100000 行：{sql}");
        return data["rows"]!.AsArray().Select(row => row!.AsArray().Select(Value).ToArray()).ToList();
    }

    public IReadOnlyList<long> Batch(IReadOnlyList<DbStep> steps)
        => Exec(steps).Select(step => step["lastRowId"]!.GetValue<long>()).ToList();

    public void Dispose()
    {
    }

    private List<JsonObject> Exec(IReadOnlyList<DbStep> steps)
    {
        var array = new JsonArray();
        for (var i = 0; i < steps.Count; i++)
            array.Add(new JsonObject { ["sql"] = steps[i].Sql, ["args"] = JsonNode.Parse(Args(steps[i].Args, i)) });
        var data = Invoke($"diana.z.exec path={Q(address)} steps={Q(array.ToJsonString())}");
        return data["steps"]!.AsArray().Select(step => step!.AsObject()).ToList();
    }

    private JsonObject Invoke(string command)
    {
        var result = bus.InvokeAsync(command, "").GetAwaiter().GetResult();
        if (!result.Success)
            throw new SqliteException($"用户数据库 {address} 访问失败（需要源码区的 HistoryDiana 3.1+）：{result.Message}");
        var node = result.Data switch
        {
            JsonElement element => JsonNode.Parse(element.GetRawText()),
            string text => JsonNode.Parse(text),
            null => null,
            var other => JsonSerializer.SerializeToNode(other),
        };
        return node as JsonObject ?? throw new SqliteException($"{command.Split(' ')[0]} 没有返回数据");
    }

    private static string Args(object?[] args, int step)
    {
        var array = new JsonArray();
        foreach (var arg in args)
        {
            array.Add(arg switch
            {
                null => null,
                bool b => b ? 1 : 0,
                int i => i,
                long l => l,
                double d => d,
                string s => s,
                byte[] blob => new JsonObject { ["base64"] = Convert.ToBase64String(blob) },
                RowRef r when r.Step < step => new JsonObject { ["rowid"] = r.Step },
                _ => throw new SqliteException($"不支持的参数 {arg}"),
            });
        }

        return array.ToJsonString();
    }

    private static object? Value(JsonNode? node) => node switch
    {
        null => null,
        JsonObject o when o["base64"] is JsonValue b => Convert.FromBase64String(b.GetValue<string>()),
        JsonValue v when v.GetValueKind() == JsonValueKind.String => v.GetValue<string>(),
        JsonValue v when v.GetValueKind() == JsonValueKind.Number =>
            // 三元两边要先装箱成 object，否则 long 会被提升成 double。
            v.ToJsonString().IndexOfAny(['.', 'e', 'E']) < 0 && long.TryParse(v.ToJsonString(), out var whole) ? (object)whole : v.GetValue<double>(),
        _ => throw new SqliteException($"无法识别的值 {node.ToJsonString()}"),
    };

    private static string Q(string text) => CommandParser.QuoteArg(text);
}
