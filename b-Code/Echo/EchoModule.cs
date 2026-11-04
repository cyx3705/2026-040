using System.Text.Json;
using System.Text.Json.Nodes;
using Echo.Board;
using Echo.Learning;
using HistoryVulcan.Core.Commands;
using HistoryVulcan.Core.Logging;
using HistoryVulcan.Core.Modules;

namespace Echo;

/// <summary>
/// 模块装配入口。界面是 Aurora 页面（左会话历史、右气泡对话，见 <see cref="EchoPage"/>）；
/// 浏览器扩展与旧网页仍连 http://127.0.0.1:8040（宿主在跑它就在跑）。推理一律经 Ratio 的指令。
/// </summary>
public sealed class EchoModule : IModuleContextAware, IDisposable
{
    private const string Hidden = "Aurora 页面内部协议，不对远程消费面暴露";

    private EchoService? _service;
    private EchoBoard? _board;

    /// <summary>宿主在装载时注入总线与登记口；正式服务模式下后台起网页服务。</summary>
    public void Attach(IModuleContext context)
    {
        ArgumentNullException.ThrowIfNull(context);
        IModuleLog? log = null;
        try
        {
            log = context.Log;
        }
        catch (NotSupportedException)
        {
        }

        void Write(string message) => log?.Info(EchoIdentity.Domain + ".app", message);
        var service = new EchoService(context.Bus, Write);
        _service = service;
        var board = new EchoBoard(service.AppAsync, context.Bus, Write);
        _board = board;
        context.RegisterCommands(registry => Register(registry, service, board));

        HostRunMode mode;
        try
        {
            mode = context.Environment.RunMode;
        }
        catch (NotSupportedException)
        {
            return;
        }

        if (mode != HostRunMode.Service || Environment.GetEnvironmentVariable("ECHO_AUTOSTART") == "0")
            return;
        _ = Task.Run(async () =>
        {
            // 等本轮装载提交、vulcan.host.info 可用后再起；热装时同样走这里。
            await Task.Delay(1500).ConfigureAwait(false);
            try
            {
                await service.StartAsync(EchoService.EnvironmentPort(), false, CancellationToken.None).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                log?.Warn(EchoIdentity.Domain + ".app", "网页服务未启动：" + ex.Message);
            }
        });
    }

    /// <summary>热重载拆除旧实例时关掉网页服务与数据库连接。</summary>
    public void Dispose()
    {
        Interlocked.Exchange(ref _board, null)?.Dispose();
        Interlocked.Exchange(ref _service, null)?.Dispose();
    }

    private static string Ok(object value) => JsonSerializer.Serialize(value, Json.Options);

    internal static void Register(ICommandRegistrar registry, EchoService service, EchoBoard board)
    {
        const string domain = EchoIdentity.Domain;
        RegisterBoard(registry, board);

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".app.status",
            Domain = domain,
            CommandClass = "app",
            Summary = "查看 Echo：网页服务地址与是否在跑、数据位置、词典是否装好、当前模型与本机推理服务状态。",
            Example = domain + ".app.status",
            Readonly = true,
            Handler = async context =>
            {
                var app = await service.AppAsync(context.Cancellation).ConfigureAwait(false);
                var status = await app.StatusAsync(context.Cancellation).ConfigureAwait(false);
                var host = service.Host;
                var data = new JsonObject
                {
                    ["running"] = host is not null, ["url"] = host?.Url, ["shared"] = host?.Shared ?? false, ["error"] = service.LastError,
                    ["userDatabase"] = app.Paths.UserDatabase, ["dataDirectory"] = app.Paths.DataDirectory,
                    ["dictionary"] = app.Dictionary.Installed, ["model"] = status["model"]?.DeepClone(),
                    ["inference"] = status["state"]?.DeepClone(), ["detail"] = status["detail"]?.DeepClone(),
                };
                var head = host is null ? $"网页服务没在跑{(service.LastError is null ? "" : "：" + service.LastError)}" : $"网页服务 {host.Url}";
                return CommandResult.Ok($"{head}；推理 {status["state"]}（{status["detail"]}）；模型 {status["model"]}；词典{(app.Dictionary.Installed ? "已装" : "未装（echo.dict.setup）")}",
                    Json.Serialize(data));
            },
        });

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".app.start",
            Domain = domain,
            CommandClass = "app",
            Summary = "启动 Echo 网页服务（宿主启动时已自动启动）；share=true 时向局域网共享扩展接口，远端要带共享密钥。",
            Example = domain + ".app.start port=8040",
            Parameters =
            [
                new ParameterSpec { Name = "port", Description = "监听端口，1-65535；省略时取环境变量 ECHO_WEB_PORT，再省略为 8040。", Type = ParamType.Int },
                new ParameterSpec { Name = "share", Description = "true 时监听全部地址供其他设备的扩展连接（需管理员 URL 权限）；false 只听本机。", Type = ParamType.Bool, Default = "false" },
            ],
            Handler = async context =>
            {
                var port = context.Has("port") ? context.GetInt("port", EchoService.DefaultPort) : EchoService.EnvironmentPort();
                if (port is < 1 or > 65535)
                    return CommandResult.Fail("port 须在 1-65535 之间");
                try
                {
                    var host = await service.StartAsync(port, context.GetBool("share", false), context.Cancellation).ConfigureAwait(false);
                    return CommandResult.Ok($"Echo 网页服务在 {host.Url}", Ok(new { url = host.Url, shared = host.Shared }));
                }
                catch (InvalidOperationException ex)
                {
                    return CommandResult.Fail(ex.Message);
                }
            },
        });

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".app.stop",
            Domain = domain,
            CommandClass = "app",
            Summary = "停止 Echo 网页服务（页面与扩展随即连不上）；学习数据不受影响。",
            Example = domain + ".app.stop",
            Handler = CommandDescriptor.Sync(_ => CommandResult.Ok(service.Stop() ? "网页服务已停止" : "网页服务本来就没在跑")),
        });

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".app.open",
            Domain = domain,
            CommandClass = "app",
            Summary = "用默认浏览器打开 Echo 页面；网页服务没在跑时先启动。",
            Example = domain + ".app.open",
            Handler = async context =>
            {
                try
                {
                    var host = service.Host ?? await service.StartAsync(EchoService.EnvironmentPort(), false, context.Cancellation).ConfigureAwait(false);
                    EchoService.OpenBrowser(host.Url);
                    return CommandResult.Ok($"已打开 {host.Url}");
                }
                catch (InvalidOperationException ex)
                {
                    return CommandResult.Fail(ex.Message);
                }
            },
        });

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".dict.setup",
            Domain = domain,
            CommandClass = "dict",
            Summary = "下载 ECDICT 英汉词典（约 66MB，分段续传）并建成离线查词库；已装好则直接返回。",
            Example = domain + ".dict.setup",
            Handler = async context =>
            {
                var app = await service.AppAsync(context.Cancellation).ConfigureAwait(false);
                if (app.Dictionary.Installed)
                    return CommandResult.Ok($"词典已在 {app.Paths.DictionaryDatabase}");
                var rows = await EchoService.SetupDictionaryAsync(app.Paths.DictionaryDatabase, context.Progress, context.Cancellation).ConfigureAwait(false);
                return CommandResult.Ok($"词典就绪：{rows} 条");
            },
        });

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".input.build",
            Domain = domain,
            CommandClass = "input",
            Summary = "编译本机中文输入桥（C++，需 VS C++ Build Tools 与 Windows SDK）；已编译过的同版本直接复用。",
            Example = domain + ".input.build",
            Handler = async context =>
            {
                var app = await service.AppAsync(context.Cancellation).ConfigureAwait(false);
                try
                {
                    var dll = await Task.Run(() => app.Input.EnsureBuilt(context.Progress), context.Cancellation).ConfigureAwait(false);
                    return CommandResult.Ok($"输入桥就绪：{dll}");
                }
                catch (InvalidOperationException ex)
                {
                    return CommandResult.Fail(ex.Message);
                }
            },
        });

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".word.list",
            Domain = domain,
            CommandClass = "word",
            Summary = "列出已学会的词（原形与短释义），最近学会的在前。",
            Example = domain + ".word.list",
            Readonly = true,
            Handler = async context =>
            {
                var app = await service.AppAsync(context.Cancellation).ConfigureAwait(false);
                var learned = app.Dictionary.Learned();
                return CommandResult.Ok($"已学会 {learned.Count} 个词：" + string.Join("、", learned.Take(20).Select(w => w.Lemma)),
                    Ok(learned.Select(w => new { lemma = w.Lemma, gloss = w.Gloss })));
            },
        });

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".word.learn",
            Domain = domain,
            CommandClass = "word",
            Summary = "把一个词标为学会（页面上不再带中文），learned=false 时取消；按原形记。",
            Example = domain + ".word.learn word=went",
            Parameters =
            [
                new ParameterSpec { Name = "word", Description = "英文单词，变形会归到原形，例如 went 记为 go。", Required = true, Position = 0 },
                new ParameterSpec { Name = "learned", Description = "true 标为学会，false 取消。", Type = ParamType.Bool, Default = "true" },
            ],
            Handler = async context =>
            {
                var app = await service.AppAsync(context.Cancellation).ConfigureAwait(false);
                var word = context.RequireString("word").Trim();
                var lemma = app.Dictionary.Lookup(word)?.Lemma ?? word.ToLowerInvariant();
                var learned = context.GetBool("learned", true);
                app.Dictionary.SetLearned(lemma, learned);
                return CommandResult.Ok(learned ? $"已学会 {lemma}" : $"已取消学会 {lemma}");
            },
        });
    }

    private static CommandDescriptor Internal(string method, string summary, Func<CommandContext, Task<CommandResult>> handler,
        ParameterSpec[]? parameters = null) => new()
    {
        Name = EchoIdentity.Domain + ".ui." + method,
        Domain = EchoIdentity.Domain,
        CommandClass = "ui",
        Summary = summary,
        Example = EchoIdentity.Domain + ".ui." + method,
        Readonly = true,
        HiddenReason = Hidden,
        Parameters = parameters ?? [],
        Handler = handler,
    };

    private static CommandResult Page(string json) => CommandResult.Ok(json, json);

    private static async Task<CommandResult> Guard(Func<Task<CommandResult>> body)
    {
        try
        {
            return await body().ConfigureAwait(false);
        }
        catch (InvalidOperationException ex)
        {
            return CommandResult.Fail(ex.Message);
        }
        catch (OperationCanceledException)
        {
            return CommandResult.Fail("已取消");
        }
    }

    private static bool TryId(string? text, out long id) => long.TryParse(text?.Trim(), out id) && id > 0;

    /// <summary>Aurora 页面三条内部指令，加历史与对话的动作指令。</summary>
    internal static void RegisterBoard(ICommandRegistrar registry, EchoBoard board)
    {
        const string domain = EchoIdentity.Domain;

        registry.Register(Internal("describe", "返回会话历史与对话两页的页面描述",
            async context => Page(EchoPage.Describe(EchoBoard.LevelLabel(await board.LevelAsync(context.Cancellation).ConfigureAwait(false))))));
        registry.Register(Internal("actions", "返回两页的动作声明", _ => Task.FromResult(Page(EchoPage.Actions()))));
        registry.Register(Internal("data", "返回页面行集：会话卡片、气泡消息（含生成中的草稿）、难度候选",
            context => Guard(async () =>
            {
                switch (context.GetString("view")?.Trim().ToLowerInvariant())
                {
                    case EchoPage.ViewLevel:
                        return CommandResult.Ok("难度", EchoBoard.LevelRows());
                    case EchoPage.ViewConversation:
                        return CommandResult.Ok("对话", await board.ConversationRowsAsync(context.GetString("q"), context.Cancellation).ConfigureAwait(false));
                    case EchoPage.ViewMessage:
                        // 还没选对话：给空表，不算失败（不往控制台刷红字）
                        if (!TryId(context.GetString("conversation"), out var id))
                            return CommandResult.Ok("消息", Array.Empty<IReadOnlyDictionary<string, string>>());
                        return CommandResult.Ok("消息", await board.MessageRowsAsync(id, context.Cancellation).ConfigureAwait(false));
                    default:
                        return CommandResult.Fail($"未知 view；支持 {EchoPage.ViewConversation} / {EchoPage.ViewMessage} / {EchoPage.ViewLevel}");
                }
            }),
            [
                new ParameterSpec
                {
                    Name = "view", Description = "取哪组行：conversation 会话卡片、message 气泡消息、level 难度候选。", Position = 0,
                    AllowedValues = [EchoPage.ViewConversation, EchoPage.ViewMessage, EchoPage.ViewLevel],
                },
                new ParameterSpec { Name = "q", Description = "会话搜索词，在标题和全部消息里找；空串或省略列出全部。" },
                new ParameterSpec { Name = "conversation", Description = "消息视图的对话编号；没选中时给空表。" },
            ]));

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".conv.list",
            Domain = domain,
            CommandClass = "conv",
            Summary = "列出对话历史（最近的在前）：编号、标题、最后一句与时间；q 在标题和全部消息里搜。",
            Example = domain + ".conv.list q=bank",
            Readonly = true,
            Parameters = [new ParameterSpec { Name = "q", Description = "搜索词，例如 bank；省略列出全部。", Position = 0 }],
            Handler = context => Guard(async () =>
            {
                var rows = await board.ConversationRowsAsync(context.GetString("q"), context.Cancellation).ConfigureAwait(false);
                return CommandResult.Ok($"共 {rows.Count} 段对话", rows);
            }),
        });

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".conv.show",
            Domain = domain,
            CommandClass = "conv",
            Summary = "看一段对话的全部消息；AI 的每句后面附逐词释义行。",
            Example = domain + ".conv.show id=12",
            Readonly = true,
            Parameters = [new ParameterSpec { Name = "id", Description = "对话编号，取 echo.conv.list 的 id。", Type = ParamType.Int, Required = true, Position = 0 }],
            Handler = context => Guard(async () =>
            {
                if (!TryId(context.RequireString("id"), out var id))
                    return CommandResult.Fail("id 要是正整数");
                var rows = await board.MessageRowsAsync(id, context.Cancellation).ConfigureAwait(false);
                return CommandResult.Ok($"{rows.Count} 条消息", rows);
            }),
        });

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".conv.new",
            Domain = domain,
            CommandClass = "conv",
            Summary = "新建一段空对话放在历史最上面（已有一段还没说话的就复用它），返回编号。",
            Example = domain + ".conv.new",
            Handler = context => Guard(async () =>
            {
                var id = await board.NewConversationAsync(context.Cancellation).ConfigureAwait(false);
                return CommandResult.Ok($"新对话 {id}：在左侧选中它开始说", Ok(new { id }));
            }),
        });

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".conv.delete",
            Domain = domain,
            CommandClass = "conv",
            Summary = "删除一段对话及其全部消息，删了找不回来；正在回复的那段要先停止。",
            Example = domain + ".conv.delete id=12",
            Parameters = [new ParameterSpec { Name = "id", Description = "对话编号，取 echo.conv.list 的 id。", Type = ParamType.Int, Required = true, Position = 0 }],
            Handler = context => Guard(async () =>
            {
                if (!TryId(context.RequireString("id"), out var id))
                    return CommandResult.Fail("id 要是正整数");
                return await board.DeleteAsync(id, context.Cancellation).ConfigureAwait(false)
                    ? CommandResult.Ok($"已删除对话 {id}")
                    : CommandResult.Fail($"没有编号 {id} 的对话");
            }),
        });

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".chat.send",
            Domain = domain,
            CommandClass = "chat",
            Summary = "在一段对话里说一句英语，AI 按当前难度回复并逐词选释义，说完存进历史；默认发出即返回，回复在页面上边生成边显示。",
            Example = domain + ".chat.send conversation=12 text=\"I went to the bank yesterday.\" wait=true",
            Parameters =
            [
                new ParameterSpec { Name = "conversation", Description = "对话编号，取 echo.conv.list 的 id，或 echo.conv.new 新建。", Type = ParamType.Int, Required = true },
                new ParameterSpec { Name = "text", Description = "你说的话（英语，夹中文也行）。", Required = true },
                new ParameterSpec { Name = "wait", Description = "true 时等 AI 说完，把回复与释义一起返回；false 发出即返回。", Type = ParamType.Bool, Default = "false" },
            ],
            Handler = context => Guard(async () =>
            {
                if (!TryId(context.RequireString("conversation"), out var id))
                    return CommandResult.Fail("conversation 要是正整数");
                var finished = await board.SendAsync(id, context.RequireString("text"), context.Cancellation).ConfigureAwait(false);
                if (!context.GetBool("wait", false))
                    return CommandResult.Ok("已发送，AI 正在回复");
                var reply = await finished.ConfigureAwait(false);
                var rows = await board.MessageRowsAsync(id, CancellationToken.None).ConfigureAwait(false);
                return CommandResult.Ok(rows.Count > 0 ? rows[^1]["text"] : reply, Ok(new { conversation = id, reply }));
            }),
        });

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".chat.stop",
            Domain = domain,
            CommandClass = "chat",
            Summary = "停掉正在生成的回复；没说完的这一轮不进历史。",
            Example = domain + ".chat.stop",
            Handler = CommandDescriptor.Sync(_ => CommandResult.Ok(board.Stop() ? "已停止" : "没有正在生成的回复")),
        });

        registry.Register(new CommandDescriptor
        {
            Name = domain + ".chat.level",
            Domain = domain,
            CommandClass = "chat",
            Summary = "设 AI 说话的难度（CEFR A1～C2+、CET-4/6、专四/专八、雅思 4.0～9.0），下一句起生效；省略只看当前。",
            Example = domain + ".chat.level level=B1",
            Parameters = [new ParameterSpec { Name = "level", Description = "难度显示名，例如 A2、CET-4、雅思 6.5；省略时只返回当前难度。", Position = 0 }],
            Handler = context => Guard(async () =>
            {
                var value = context.GetString("level");
                if (string.IsNullOrWhiteSpace(value))
                    return CommandResult.Ok("当前难度 " + EchoBoard.LevelLabel(await board.LevelAsync(context.Cancellation).ConfigureAwait(false)));
                var label = await board.SetLevelAsync(value, context.Cancellation).ConfigureAwait(false);
                return label is null
                    ? CommandResult.Fail("认不出这个难度；可选：" + string.Join("、", Levels.Choices().Select(c => c.Label)))
                    : CommandResult.Ok("难度改为 " + label);
            }),
        });
    }
}
