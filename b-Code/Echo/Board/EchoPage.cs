using System.Text.Encodings.Web;
using System.Text.Json;

namespace Echo.Board;

/// <summary>
/// Aurora 页面协议 V1：左侧会话历史（卡片 + 搜索 + 新对话/删除），右侧气泡对话与发送栏。组件与 HistoryFama 同一套。
/// </summary>
/// <remarks>
/// 两页只靠选择通道 <see cref="ConversationChannel"/> 接线：历史页发布选中的对话，对话页按它取数、往它发。
/// owner 与 scene 用 <see cref="EchoIdentity.PageOwner"/>；按钮、开关只绑 <see cref="Actions()"/> 里声明的动作 id，
/// 动作指向的指令都由本模块注册（离线测试守着）。
/// </remarks>
internal static class EchoPage
{
    public const string HistoryPageId = EchoIdentity.Domain + "-history";
    public const string ChatPageId = EchoIdentity.Domain + "-chat";
    public const string ToolPanelId = EchoIdentity.Domain + "-tools";
    public const string SendPanelId = EchoIdentity.Domain + "-send";

    /// <summary>会话卡片节点 id；对话说完、起好标题、新建或删除后重取它。</summary>
    public const string ConversationListId = EchoIdentity.Domain + "-conversations";

    /// <summary>气泡时间线节点 id；生成中按节拍重取。</summary>
    public const string MessageListId = EchoIdentity.Domain + "-messages";

    public const string ConversationChannel = EchoIdentity.Domain + ".conversation";
    public const string SearchChannel = EchoIdentity.Domain + ".search";

    public const string TextBoxId = "text";
    public const string LevelBoxId = "level";

    public const string ViewConversation = "conversation";
    public const string ViewMessage = "message";
    public const string ViewLevel = "level";

    public const string NewActionId = EchoIdentity.Domain + ".conv.new";
    public const string DeleteActionId = EchoIdentity.Domain + ".conv.delete";
    public const string SendActionId = EchoIdentity.Domain + ".chat.send";
    public const string StopActionId = EchoIdentity.Domain + ".chat.stop";
    public const string LevelActionId = EchoIdentity.Domain + ".chat.level";

    public const string DataCommand = EchoIdentity.Domain + ".ui.data";

    private static readonly JsonSerializerOptions Options = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    private static string Selected(string column) => "{selection." + ConversationChannel + "." + column + "}";

    /// <summary>页面描述；<paramref name="level"/> 是难度选项框的初值（显示名）。</summary>
    public static string Describe(string level) => JsonSerializer.Serialize(new
    {
        schemaVersion = 1,
        owner = EchoIdentity.PageOwner,
        pages = new object[]
        {
            new
            {
                id = HistoryPageId,
                title = "会话历史",
                scene = EchoIdentity.PageOwner,
                placement = new { side = "left", ratio = 0.28, visible = true, singleton = true },
                content = new
                {
                    type = "stack",
                    gap = "tight",
                    children = new object[]
                    {
                        new
                        {
                            type = "panel",
                            id = ToolPanelId,
                            text = "Echo",
                            rows = new object[]
                            {
                                new
                                {
                                    widgets = new object[]
                                    {
                                        new { kind = "textbox", id = "query", label = "搜索", channel = SearchChannel, value = "", flex = true },
                                        new { kind = "button", action = NewActionId, text = "新对话" },
                                        new { kind = "button", action = DeleteActionId, text = "删除", enabledWhen = new { selected = ConversationChannel } },
                                    },
                                },
                            },
                        },
                        new
                        {
                            type = "conversationList",
                            id = ConversationListId,
                            channel = ConversationChannel,
                            dataSource = new
                            {
                                command = DataCommand,
                                args = new Dictionary<string, string>
                                {
                                    ["view"] = ViewConversation, ["q"] = "{selection." + SearchChannel + ".value}",
                                },
                            },
                        },
                    },
                },
            },
            new
            {
                id = ChatPageId,
                title = "对话",
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
                            type = "chatTimeline",
                            id = MessageListId,
                            source = Selected("title"),
                            text = "选左边一段对话，或点「新对话」",
                            dataSource = new
                            {
                                command = DataCommand,
                                args = new Dictionary<string, string> { ["view"] = ViewMessage, ["conversation"] = Selected("id") },
                            },
                        },
                        new
                        {
                            type = "panel",
                            id = SendPanelId,
                            text = "说英语",
                            rows = new object[]
                            {
                                new
                                {
                                    widgets = new object[]
                                    {
                                        new { kind = "textbox", id = TextBoxId, label = "", multiline = true, flex = true },
                                    },
                                },
                                new
                                {
                                    widgets = new object[]
                                    {
                                        new
                                        {
                                            kind = "textbox", id = LevelBoxId, label = "难度", mode = "select", value = level, minWidth = 96,
                                            commitAction = LevelActionId,
                                            optionsSource = new { command = DataCommand, args = new { view = ViewLevel } },
                                        },
                                        new { kind = "text", text = "Enter 换行 · AI 回复带逐词释义", flex = true },
                                        new { kind = "button", action = StopActionId, text = "停止" },
                                        new { kind = "button", action = SendActionId, text = "发送", enabledWhen = new { selected = ConversationChannel } },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }, Options);

    /// <summary>动作声明。</summary>
    public static string Actions() => JsonSerializer.Serialize(new
    {
        schemaVersion = 1,
        owner = EchoIdentity.PageOwner,
        actions = new object[]
        {
            new
            {
                id = NewActionId, title = "新对话", command = NewActionId, args = new Dictionary<string, string>(),
                summary = "在左侧最上面放一段空对话（已有空的就复用），选中它开始说",
            },
            new
            {
                id = DeleteActionId, title = "删除", command = DeleteActionId, danger = true,
                args = new Dictionary<string, string> { ["id"] = Selected("id") },
                summary = "删除选中的对话，删了找不回来",
            },
            new
            {
                id = SendActionId, title = "发送", command = SendActionId,
                args = new Dictionary<string, string> { ["conversation"] = Selected("id"), ["text"] = "{" + TextBoxId + "}" },
                clearOnSuccess = new[] { TextBoxId },
                summary = "把这句话发给 AI；回复边生成边显示，说完附上逐词释义",
            },
            new
            {
                id = StopActionId, title = "停止", command = StopActionId, args = new Dictionary<string, string>(),
                summary = "停掉正在生成的回复（没说完的这一轮不进历史）",
            },
            new
            {
                id = LevelActionId, title = "难度", command = LevelActionId,
                args = new Dictionary<string, string> { ["level"] = "{value}" },
                summary = "AI 说话的难度，下一句起生效",
            },
        },
    }, Options);
}
