using HistoryVulcan.Core.Commands;
using HistoryVulcan.Core.Modules;

namespace Echo;

/// <summary>
/// 模块装配入口：业务指令 + Aurora 页面协议三条指令（describe / actions / data）。
/// </summary>
/// <remarks>
/// 模板里的 hello.* 是示范：一条改状态的无参指令（页面按钮）、一条带参数的只读指令（控制台）、
/// 一条只读列表（MCP）。实例化后换成真实业务，但保留这三种形态的写法：
/// 改东西的不标 <c>Readonly</c>；只读查询标 <c>Readonly</c>（mcpExposure=readonly 时只有它们投影到 MCP）；
/// 界面内部协议一律带 <c>HiddenReason</c>。
/// </remarks>
public sealed class EchoModule : IModuleContextAware
{
    private const string Hidden = "Aurora 页面内部协议，不对远程消费面暴露";

    /// <summary>宿主在装载时注入权威指令总线与命令注册器。</summary>
    public void Attach(IModuleContext context)
    {
        ArgumentNullException.ThrowIfNull(context);
        var state = new EchoState(context.Bus);
        context.RegisterCommands(registry => Register(registry, state));
    }

    internal static void Register(ICommandRegistrar registry, EchoState state)
    {
        registry.Register(new CommandDescriptor
        {
            Name = EchoIdentity.Domain + ".hello.ping",
            Domain = EchoIdentity.Domain,
            CommandClass = "hello",
            Summary = "打一声招呼，记进页面表格",
            Example = EchoIdentity.Domain + ".hello.ping",
            Level = CommandLevel.Run,
            Handler = async context =>
            {
                context.Progress?.Report("正在打招呼…");
                return CommandResult.Ok(await state.PingAsync().ConfigureAwait(false));
            },
        });

        registry.Register(new CommandDescriptor
        {
            Name = EchoIdentity.Domain + ".hello.echo",
            Domain = EchoIdentity.Domain,
            CommandClass = "hello",
            Summary = "原样返回一段文本",
            Example = EchoIdentity.Domain + ".hello.echo text=OneHistory",
            Readonly = true,
            Parameters =
            [
                new ParameterSpec
                {
                    Name = "text",
                    Description = "要原样返回的文本，任意字符串，例如 OneHistory",
                    Type = ParamType.String,
                    Required = true,
                },
            ],
            Handler = CommandDescriptor.Sync(context => CommandResult.Ok(context.RequireString("text"))),
        });

        registry.Register(new CommandDescriptor
        {
            Name = EchoIdentity.Domain + ".hello.list",
            Domain = EchoIdentity.Domain,
            CommandClass = "hello",
            Summary = "列出招呼记录",
            Example = EchoIdentity.Domain + ".hello.list",
            Readonly = true,
            Handler = CommandDescriptor.Sync(_ => CommandResult.Ok($"共 {state.Count} 次招呼", state.Rows())),
        });

        registry.Register(Internal("describe", "返回页面描述", _ => Json(EchoPage.Describe())));
        registry.Register(Internal("actions", "返回页面动作声明", _ => Json(EchoPage.Actions())));
        registry.Register(new CommandDescriptor
        {
            Name = EchoIdentity.Domain + ".ui.data",
            Domain = EchoIdentity.Domain,
            CommandClass = "ui",
            Summary = "返回页面表格的行",
            Readonly = true,
            HiddenReason = Hidden,
            AllowUnspecifiedParameters = true,
            Handler = CommandDescriptor.Sync(context =>
                context.GetString("view")?.Trim().ToLowerInvariant() is null or EchoPage.HistoryView
                    ? CommandResult.Ok("招呼记录", state.Rows())
                    : CommandResult.Fail($"未知 view；支持 {EchoPage.HistoryView}")),
        });
    }

    private static CommandResult Json(string json) => CommandResult.Ok(json, json);

    private static CommandDescriptor Internal(string method, string summary, Func<CommandContext, CommandResult> handler)
        => new()
        {
            Name = EchoIdentity.Domain + ".ui." + method,
            Domain = EchoIdentity.Domain,
            CommandClass = "ui",
            Summary = summary,
            Readonly = true,
            HiddenReason = Hidden,
            Handler = CommandDescriptor.Sync(handler),
        };
}
