using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using Echo.App;
using Echo.Inference;
using Echo.Web;
using HistoryVulcan.Core.Commands;

namespace Echo;

/// <summary>
/// 一个模块实例里的 Echo：解析数据位置、持有后端（<see cref="EchoApp"/>）与网页服务（<see cref="HttpHost"/>）。
/// </summary>
internal sealed class EchoService : IDisposable
{
    public const int DefaultPort = 8040;

    private readonly ICommandBus _bus;
    private readonly Action<string> _log;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private EchoApp? _app;
    private HttpHost? _host;

    public EchoService(ICommandBus bus, Action<string> log)
    {
        _bus = bus;
        _log = log;
    }

    public EchoPaths? Paths { get; private set; }

    public string? LastError { get; private set; }

    public HttpHost? Host => _host;

    public EchoApp? App => _app;

    public static int EnvironmentPort()
        => int.TryParse(Environment.GetEnvironmentVariable("ECHO_WEB_PORT"), out var port) && port is > 0 and < 65536 ? port : DefaultPort;

    /// <summary>后端（数据库、词典）按需建立；网页服务停了也能查学会的词。</summary>
    public async Task<EchoApp> AppAsync(CancellationToken cancellation)
    {
        if (_app is { } ready)
            return ready;
        await _gate.WaitAsync(cancellation).ConfigureAwait(false);
        try
        {
            if (_app is not null)
                return _app;
            string? library = null;
            var info = await _bus.InvokeAsync("vulcan.host.info", "", cancellation).ConfigureAwait(false);
            if (info.Success && RatioInference.DataOf(info) is JsonObject data)
                library = data["libraryRoot"]?.GetValue<string>();
            Paths = EchoPaths.Resolve(library);
            _app = new EchoApp(Paths, new RatioInference(_bus), _log);
            return _app;
        }
        finally
        {
            _gate.Release();
        }
    }

    /// <summary>起网页服务；已在跑且参数相同直接返回。</summary>
    public async Task<HttpHost> StartAsync(int port, bool share, CancellationToken cancellation)
    {
        var app = await AppAsync(cancellation).ConfigureAwait(false);
        await _gate.WaitAsync(cancellation).ConfigureAwait(false);
        try
        {
            if (_host is { } running && running.Port == port && running.Shared == share)
                return running;
            _host?.Dispose();
            _host = null;
            var key = share ? HttpHost.LoadShareKey(app.Paths.ShareKeyFile) : null;
            var host = new HttpHost(port, share, key, app.HandleAsync, _log);
            try
            {
                host.Start();
            }
            catch (System.Net.HttpListenerException ex)
            {
                host.Dispose();
                LastError = $"端口 {port} 起不来：{ex.Message}（旧的 Python 版 Echo 还开着时先关掉它）";
                throw new InvalidOperationException(LastError);
            }

            LastError = null;
            _host = host;
            _log($"网页服务已启动：{host.Url}{(share ? "（共享模式：远端需共享密钥）" : "")}");
            return host;
        }
        finally
        {
            _gate.Release();
        }
    }

    public bool Stop()
    {
        var host = Interlocked.Exchange(ref _host, null);
        host?.Dispose();
        return host is not null;
    }

    public void Dispose()
    {
        Stop();
        _app?.Dispose();
        _app = null;
    }

    public static void OpenBrowser(string url)
        => Process.Start(new ProcessStartInfo(url) { UseShellExecute = true })?.Dispose();

    /// <summary>ECDICT（MIT）：分段续传下载 CSV（本机出网大传输会断，靠 Range 加重试），再建 SQLite。</summary>
    public static async Task<int> SetupDictionaryAsync(string dbPath, IProgress<string>? progress, CancellationToken cancellation)
    {
        const string url = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv";
        var csv = Path.Combine(Path.GetDirectoryName(dbPath)!, "ecdict.csv");
        var part = csv + ".part";
        Directory.CreateDirectory(Path.GetDirectoryName(dbPath)!);
        if (!File.Exists(csv))
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
            using var head = await http.SendAsync(new HttpRequestMessage(HttpMethod.Head, url), cancellation).ConfigureAwait(false);
            var total = head.Content.Headers.ContentLength ?? throw new InvalidOperationException("ECDICT 服务器没有给出文件大小");
            var failures = 0;
            while ((File.Exists(part) ? new FileInfo(part).Length : 0) is var done && done < total)
            {
                var end = Math.Min(done + (4 << 20), total) - 1;
                try
                {
                    using var request = new HttpRequestMessage(HttpMethod.Get, url);
                    request.Headers.Range = new System.Net.Http.Headers.RangeHeaderValue(done, end);
                    request.Headers.AcceptEncoding.ParseAdd("identity");
                    using var response = await http.SendAsync(request, cancellation).ConfigureAwait(false);
                    response.EnsureSuccessStatusCode();
                    var bytes = await response.Content.ReadAsByteArrayAsync(cancellation).ConfigureAwait(false);
                    await using (var file = new FileStream(part, FileMode.Append))
                        await file.WriteAsync(bytes, cancellation).ConfigureAwait(false);
                    failures = 0;
                    progress?.Report($"下载 ECDICT {(done + bytes.Length) * 100 / total}%");
                }
                catch (HttpRequestException ex) when (++failures <= 20)
                {
                    progress?.Report($"下载中断，重试（{failures}/20）：{ex.Message}");
                }
            }

            File.Move(part, csv, overwrite: true);
        }

        progress?.Report("建立词典索引…");
        var rows = await Task.Run(() => Learning.Dictionary.Build(csv, dbPath), cancellation).ConfigureAwait(false);
        File.Delete(csv);
        return rows;
    }
}
