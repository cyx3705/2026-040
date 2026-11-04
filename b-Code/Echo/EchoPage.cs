using System.Text.Encodings.Web;
using System.Text.Json;

namespace Echo;

/// <summary>
/// Aurora 页面协议 V1：一页，上面一块控制面板放按钮，下面一张表。
/// </summary>
/// <remarks>
/// 三条规矩（离线测试守着）：owner 与 scene 用 <see cref="EchoIdentity.PageOwner"/>；
/// 每个按钮的 action 都在 <see cref="Actions()"/> 里声明；每个动作指向的指令都已注册。
/// </remarks>
internal static class EchoPage
{
    public const string PageId = EchoIdentity.Domain;
    public const string PanelId = "hello-actions";
    public const string HistoryTableId = "hello-history";
    public const string HistoryView = "history";
    public const string PingActionId = EchoIdentity.Domain + ".hello.ping";

    private static readonly JsonSerializerOptions Options = new()
    {
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    public static string Describe() => JsonSerializer.Serialize(new
    {
        schemaVersion = 1,
        owner = EchoIdentity.PageOwner,
        pages = new object[]
        {
            new
            {
                id = PageId,
                title = EchoIdentity.PageTitle,
                scene = EchoIdentity.PageOwner,
                placement = new { side = "center", visible = true, singleton = true },
                content = new
                {
                    type = "stack",
                    gap = "tight",
                    children = new object[]
                    {
                        new
                        {
                            type = "panel",
                            id = PanelId,
                            text = "操作",
                            rows = new object[]
                            {
                                new
                                {
                                    mode = "even",
                                    widgets = new object[]
                                    {
                                        new { kind = "button", action = PingActionId, text = "打招呼" },
                                    },
                                },
                            },
                        },
                        new
                        {
                            type = "text",
                            style = "caption",
                            text = "执行过程与结果同时写进控制台。",
                        },
                        new
                        {
                            type = "table",
                            id = HistoryTableId,
                            dataSource = new
                            {
                                command = EchoIdentity.Domain + ".ui.data",
                                args = new { view = HistoryView },
                            },
                            columns = new object[]
                            {
                                new { key = "index", title = "次序", width = "80" },
                                new { key = "time", title = "时间", width = "*" },
                            },
                        },
                    },
                },
            },
        },
    }, Options);

    public static string Actions() => JsonSerializer.Serialize(new
    {
        schemaVersion = 1,
        owner = EchoIdentity.PageOwner,
        actions = new object[]
        {
            new
            {
                id = PingActionId,
                title = "打招呼",
                command = EchoIdentity.Domain + ".hello.ping",
                summary = "打一声招呼，记进表格",
            },
        },
    }, Options);
}
