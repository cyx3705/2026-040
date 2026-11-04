using System.Text.Json;
using System.Text.Json.Nodes;
using Echo.Learning;
using HistoryVulcan.Core.Commands;
using HistoryVulcan.Core.Logging;
using HistoryVulcan.Core.Modules;

namespace Echo;

/// <summary>
/// 模块装配入口。Echo 的界面是自己的本机网页（逐词交互是定制界面，不进 Aurora）：
/// 宿主在跑，网页服务就在 http://127.0.0.1:8040 跑，浏览器扩展连同一个地址。推理一律经 Ratio 的指令。
/// </summary>
public sealed class EchoModule : IModuleContextAware, IDisposable
{
    private EchoService? _service;

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
        context.RegisterCommands(registry => Register(registry, service));

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
        Interlocked.Exchange(ref _service, null)?.Dispose();
    }

    private static string Ok(object value) => JsonSerializer.Serialize(value, Json.Options);

    internal static void Register(ICommandRegistrar registry, EchoService service)
    {
        const string domain = EchoIdentity.Domain;

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
}
