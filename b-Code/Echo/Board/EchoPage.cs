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
    public const string DictionaryPageId = EchoIdentity.Domain + "-dictionary";
    public const string ToolPanelId = EchoIdentity.Domain + "-tools";
    public const string SendPanelId = EchoIdentity.Domain + "-send";
    public const string DictionaryPanelId = EchoIdentity.Domain + "-dictionary-tools";
    public const string DictionaryTableId = EchoIdentity.Domain + "-dictionary-list";

    /// <summary>会话卡片节点 id；对话说完、起好标题、新建或删除后重取它。</summary>
    public const string ConversationListId = EchoIdentity.Domain + "-conversations";

    /// <summary>气泡时间线节点 id；生成中按节拍重取。</summary>
    public const string MessageListId = EchoIdentity.Domain + "-messages";

    public const string ConversationChannel = EchoIdentity.Domain + ".conversation";
    public const string SearchChannel = EchoIdentity.Domain + ".search";
    public const string ComposerChannel = EchoIdentity.Domain + ".composer";

    public const string TextBoxId = "text";
    public const string StandardBoxId = "standard";
    public const string LevelBoxId = "level";
    public const string LengthBoxId = "length";
    public const string CandidateCountBoxId = "candidate-count";

    public const string ViewConversation = "conversation";
    public const string ViewMessage = "message";
    public const string ViewSuggest = "suggest";
    public const string ViewLevel = "level";
    public const string ViewStandard = "standard";
    public const string ViewLength = "length";
    public const string ViewCandidateCount = "candidate-count";
    public const string ViewDictionary = "dictionary";

    public const string NewActionId = EchoIdentity.Domain + ".conv.new";
    public const string DeleteActionId = EchoIdentity.Domain + ".conv.delete";
    public const string SendActionId = EchoIdentity.Domain + ".chat.send";
    public const string StopActionId = EchoIdentity.Domain + ".chat.stop";
    public const string LevelActionId = EchoIdentity.Domain + ".chat.level";
    public const string StandardActionId = EchoIdentity.Domain + ".chat.standard";
    public const string LengthActionId = EchoIdentity.Domain + ".chat.length";
    public const string CandidateCountActionId = EchoIdentity.Domain + ".chat.candidates";
    public const string SuggestActionId = EchoIdentity.Domain + ".chat.suggest";
    public const string TranslateActionId = EchoIdentity.Domain + ".chat.translate";
    public const string AcceptSuggestionActionId = EchoIdentity.Domain + ".chat.accept";
    public const string LearnWordActionId = EchoIdentity.Domain + ".word.learn-from-chat";
    public const string UnlearnWordActionId = EchoIdentity.Domain + ".word.unlearn-from-dictionary";
    public const string OpenDictionaryActionId = EchoIdentity.Domain + ".dictionary.open";
    public const string CloseDictionaryActionId = EchoIdentity.Domain + ".dictionary.close";

    public const string DataCommand = EchoIdentity.Domain + ".ui.data";

    private static readonly JsonSerializerOptions Options = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    private static string Selected(string column) => "{selection." + ConversationChannel + "." + column + "}";

    /// <summary>页面描述；<paramref name="level"/> 是难度选项框的初值（显示名）。</summary>
    public static string Describe(string level, int candidateCount = 5, int learnedCount = 0) => JsonSerializer.Serialize(new
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
                            wordAction = LearnWordActionId,
                            headerActions = new[] { new { action = OpenDictionaryActionId, title = "词典" } },
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
                                        new { kind = "textbox", id = TextBoxId, label = "", multiline = true, flex = true, channel = ComposerChannel, enterAction = SendActionId },
                                    },
                                },
                                new
                                {
                                    widgets = new object[]
                                    {
                                        new
                                        {
                                            kind = "choices", id = "suggestions", label = "", flex = true,
                                            optionsSource = new { command = DataCommand, args = new Dictionary<string, string> { ["view"] = ViewSuggest, ["conversation"] = Selected("id"), ["text"] = "{selection." + ComposerChannel + ".value}" } },
                                            commitAction = AcceptSuggestionActionId,
                                            keyboard = true,
                                            keyboardTarget = TextBoxId,
                                        },
                                    },
                                },
                                new
                                {
                                    widgets = new object[]
                                    {
                                        new
                                        {
                                            kind = "textbox", id = StandardBoxId, label = "分级类型", mode = "select", value = "欧洲语言框架", minWidth = 128,
                                            commitAction = StandardActionId,
                                            optionsSource = new { command = DataCommand, args = new { view = ViewStandard } },
                                        },
                                        new
                                        {
                                            kind = "textbox", id = LevelBoxId, label = "等级", mode = "select", value = level, minWidth = 96,
                                            commitAction = LevelActionId,
                                            optionsSource = new { command = DataCommand, args = new { view = ViewLevel } },
                                        },
                                        new
                                        {
                                            kind = "textbox", id = LengthBoxId, label = "回复长度", mode = "select", value = "人类", minWidth = 104,
                                            commitAction = LengthActionId,
                                            optionsSource = new { command = DataCommand, args = new { view = ViewLength } },
                                        },
                                        new
                                        {
                                            kind = "textbox", id = CandidateCountBoxId, label = "选词数量", mode = "select",
                                            value = candidateCount.ToString(), minWidth = 96, commitAction = CandidateCountActionId,
                                            optionsSource = new { command = DataCommand, args = new { view = ViewCandidateCount } },
                                        },
                                        new { kind = "button", action = StopActionId, text = "停止" },
                                        new { kind = "button", action = SendActionId, text = "发送" },
                                    },
                                },
                            },
                        },
                    },
                },
            },
            new
            {
                id = DictionaryPageId,
                title = "词典",
                scene = EchoIdentity.PageOwner,
                placement = new { side = "right", ratio = 0.34, visible = false, singleton = true },
                content = new
                {
                    type = "stack",
                    gap = "tight",
                    children = new object[]
                    {
                        new
                        {
                            type = "panel",
                            id = DictionaryPanelId,
                            text = "词典",
                            rows = new object[]
                            {
                                new
                                {
                                    widgets = new object[]
                                    {
                                        new { kind = "text", text = "已熟单词：" + learnedCount, flex = true },
                                        new { kind = "button", action = CloseDictionaryActionId, text = "关闭" },
                                    },
                                },
                            },
                        },
                        new
                        {
                            type = "table",
                            id = DictionaryTableId,
                            dataSource = new { command = DataCommand, args = new { view = ViewDictionary } },
                            columns = new object[]
                            {
                                new { key = "lemma", title = "单词", width = "*" },
                                new { key = "gloss", title = "中文释义", width = "*" },
                            },
                            rowActions = new object[]
                            {
                                new { action = UnlearnWordActionId, title = "移出词典", style = "danger" },
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
                args = new Dictionary<string, string> { ["text"] = "{" + TextBoxId + "}" },
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
            new
            {
                id = StandardActionId, title = "分级类型", command = EchoIdentity.Domain + ".chat.level",
                args = new Dictionary<string, string> { ["standard"] = "{value}" },
                summary = "选择 CEFR、国内考试或雅思口语分级",
            },
            new
            {
                id = LengthActionId, title = "回复长度", command = EchoIdentity.Domain + ".chat.level",
                args = new Dictionary<string, string> { ["length"] = "{value}" },
                summary = "选择一句话、人类、长篇或故事等回复长度",
            },
            new
            {
                id = CandidateCountActionId, title = "选词数量", command = CandidateCountActionId,
                args = new Dictionary<string, string> { ["count"] = "{value}" }, summary = "设置输入候选显示数量（1 到 20 个）",
            },
            new
            {
                id = TranslateActionId, title = "翻译并回填", command = TranslateActionId,
                args = new Dictionary<string, string> { ["chinese"] = "{chinese}" }, summary = "按当前对话上下文从字典候选中选择英文并回填输入框",
            },
            new
            {
                id = SuggestActionId, title = "输入选词", command = SuggestActionId,
                args = new Dictionary<string, string> { ["text"] = "{" + TextBoxId + "}" }, summary = "按光标前后文显示最多 20 个英文候选及释义",
            },
            new
            {
                id = AcceptSuggestionActionId, title = "选词", command = AcceptSuggestionActionId,
                args = new Dictionary<string, string> { ["value"] = "{value}", ["text"] = "{" + TextBoxId + "}" }, summary = "把候选词插入发送框并保留末尾空格",
            },
            new
            {
                id = LearnWordActionId, title = "标为已知", command = EchoIdentity.Domain + ".word.learn",
                args = new Dictionary<string, string> { ["word"] = "{word}", ["learned"] = "true" }, summary = "点击回复中的单词后立即隐藏中文并收入词典",
            },
            new
            {
                id = UnlearnWordActionId, title = "移出词典", command = EchoIdentity.Domain + ".word.learn",
                args = new Dictionary<string, string> { ["word"] = "{lemma}", ["learned"] = "false" }, summary = "从词典移出这个已知词",
            },
            new
            {
                id = OpenDictionaryActionId, title = "词典", command = OpenDictionaryActionId,
                args = new Dictionary<string, string> { ["page"] = DictionaryPageId }, summary = "打开已知词典",
            },
            new
            {
                id = CloseDictionaryActionId, title = "关闭", command = CloseDictionaryActionId,
                args = new Dictionary<string, string> { ["page"] = DictionaryPageId }, summary = "关闭词典页面",
            },
        },
    }, Options);
}
