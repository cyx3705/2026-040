using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Echo.Learning;

namespace Echo.Web;

/// <summary>路由处理里抛出的 HTTP 错误；回给页面的是 <c>{"detail": 消息}</c>（与原 FastAPI 后端同形）。</summary>
internal sealed class HttpError(int status, string detail) : Exception(detail)
{
    public int Status { get; } = status;
}

/// <summary>一次请求。</summary>
internal sealed class EchoRequest
{
    private readonly HttpListenerContext _context;
    private byte[]? _body;

    public EchoRequest(HttpListenerContext context, CancellationToken aborted)
    {
        _context = context;
        Aborted = aborted;
    }

    public string Method => _context.Request.HttpMethod;

    public string Path => _context.Request.Url?.AbsolutePath ?? "/";

    public string? Query(string name) => _context.Request.QueryString[name];

    public string? Header(string name) => _context.Request.Headers[name];

    public string Host => _context.Request.Url?.Host ?? "";

    public string Origin => $"{_context.Request.Url?.Scheme}://{_context.Request.Url?.Authority}";

    public IPAddress? Client => _context.Request.RemoteEndPoint?.Address;

    /// <summary>连接断开或服务停止时取消。</summary>
    public CancellationToken Aborted { get; }

    public HttpListenerResponse Response => _context.Response;

    public async Task<byte[]> BodyAsync()
    {
        if (_body is not null)
            return _body;
        using var buffer = new MemoryStream();
        await _context.Request.InputStream.CopyToAsync(buffer, Aborted).ConfigureAwait(false);
        return _body = buffer.ToArray();
    }

    public async Task<JsonObject> JsonAsync()
    {
        var bytes = await BodyAsync().ConfigureAwait(false);
        try
        {
            return JsonNode.Parse(bytes) as JsonObject ?? throw new HttpError(422, "请求体必须是 JSON 对象");
        }
        catch (System.Text.Json.JsonException ex)
        {
            throw new HttpError(422, "请求体不是合法 JSON：" + ex.Message);
        }
    }
}

/// <summary>SSE 写出器：一个事件一行 <c>data:</c>。连接断开时 <see cref="SendAsync"/> 返回 false。</summary>
internal sealed class SseWriter(HttpListenerResponse response)
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private bool _started;
    private bool _broken;

    public bool Broken => _broken;

    public async Task<bool> SendAsync(JsonNode payload)
    {
        if (_broken)
            return false;
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (!_started)
            {
                response.StatusCode = 200;
                response.ContentType = "text/event-stream; charset=utf-8";
                response.SendChunked = true;
                response.Headers["Cache-Control"] = "no-cache";
                response.Headers["X-Accel-Buffering"] = "no";
                _started = true;
            }

            var bytes = Encoding.UTF8.GetBytes("data: " + Json.Serialize(payload) + "\n\n");
            await response.OutputStream.WriteAsync(bytes).ConfigureAwait(false);
            await response.OutputStream.FlushAsync().ConfigureAwait(false);
            return true;
        }
        catch (Exception ex) when (ex is HttpListenerException or IOException or ObjectDisposedException or InvalidOperationException)
        {
            _broken = true;
            return false;
        }
        finally
        {
            _gate.Release();
        }
    }
}

/// <summary>
/// Echo 的本机网页服务（NET-001 / 原 echo-serve）：页面、页面 API 与两款扩展接口。
/// </summary>
/// <remarks>
/// 只监听回环地址；共享模式（<c>share=true</c>）监听全部地址，远端请求必须带 <c>Bearer 共享密钥</c>，
/// 且只放行扩展要用的四个接口。本机页面与扩展（<c>chrome-extension://</c>）免密钥。
/// </remarks>
internal sealed class HttpHost : IDisposable
{
    private static readonly HashSet<(string, string)> ExtensionRoutes =
    [
        ("GET", "/api/status"),
        ("GET", "/api/reader/learned"),
        ("POST", "/api/reader/gloss"),
        ("POST", "/api/learned"),
    ];

    private readonly HttpListener _listener = new();
    private readonly Func<EchoRequest, Task> _handle;
    private readonly Action<string> _log;
    private readonly CancellationTokenSource _stop = new();
    private readonly string? _shareKey;
    private Task? _loop;

    public HttpHost(int port, bool share, string? shareKey, Func<EchoRequest, Task> handle, Action<string> log)
    {
        Port = port;
        Shared = share;
        _shareKey = shareKey;
        _handle = handle;
        _log = log;
        if (share)
        {
            _listener.Prefixes.Add($"http://+:{port}/");
        }
        else
        {
            _listener.Prefixes.Add($"http://127.0.0.1:{port}/");
            _listener.Prefixes.Add($"http://localhost:{port}/");
        }
    }

    public int Port { get; }

    public bool Shared { get; }

    public string Url => $"http://127.0.0.1:{Port}";

    public void Start()
    {
        _listener.Start();
        _loop = Task.Run(AcceptAsync);
    }

    public void Dispose()
    {
        _stop.Cancel();
        try
        {
            _listener.Stop();
            _listener.Close();
        }
        catch (ObjectDisposedException)
        {
        }
    }

    private async Task AcceptAsync()
    {
        while (!_stop.IsCancellationRequested)
        {
            HttpListenerContext context;
            try
            {
                context = await _listener.GetContextAsync().ConfigureAwait(false);
            }
            catch (Exception) when (_stop.IsCancellationRequested || !_listener.IsListening)
            {
                return;
            }
            catch (HttpListenerException ex)
            {
                _log($"接收请求失败：{ex.Message}");
                continue;
            }

            _ = Task.Run(() => ServeAsync(context));
        }
    }

    private async Task ServeAsync(HttpListenerContext context)
    {
        var request = new EchoRequest(context, _stop.Token);
        try
        {
            if (!Admit(request, out var status, out var detail))
            {
                if (status == 401)
                    context.Response.Headers["WWW-Authenticate"] = "Bearer";
                context.Response.Headers["Cache-Control"] = "no-store";
                await WriteJsonAsync(context.Response, status, new JsonObject { ["detail"] = detail }).ConfigureAwait(false);
                return;
            }

            await _handle(request).ConfigureAwait(false);
        }
        catch (HttpError ex)
        {
            await TryWriteAsync(context.Response, ex.Status, new JsonObject { ["detail"] = ex.Message }).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception ex)
        {
            _log($"{request.Method} {request.Path} 出错：{ex.GetType().Name}: {ex.Message}");
            await TryWriteAsync(context.Response, 500, new JsonObject { ["detail"] = ex.Message }).ConfigureAwait(false);
        }
        finally
        {
            try
            {
                context.Response.Close();
            }
            catch (Exception)
            {
            }
        }
    }

    private bool Admit(EchoRequest request, out int status, out string detail)
    {
        status = 200;
        detail = "";
        var origin = request.Header("Origin");
        var local = request.Client is { } client && IPAddress.IsLoopback(client) && IsLoopbackHost(request.Host)
                    && (string.IsNullOrEmpty(origin) || origin == request.Origin
                        || origin.StartsWith("chrome-extension://", StringComparison.Ordinal));
        if (local)
            return true;
        var expected = _shareKey is null ? null : Encoding.UTF8.GetBytes("Bearer " + _shareKey);
        var given = Encoding.UTF8.GetBytes(request.Header("Authorization") ?? "");
        if (expected is null || !CryptographicOperations.FixedTimeEquals(expected, given))
        {
            status = 401;
            detail = "extension_auth_required";
            return false;
        }

        if (!ExtensionRoutes.Contains((request.Method, request.Path)))
        {
            status = 403;
            detail = "extension_route_only";
            return false;
        }

        return true;
    }

    internal static bool IsLoopbackHost(string host)
        => string.Equals(host, "localhost", StringComparison.OrdinalIgnoreCase)
           || (IPAddress.TryParse(host.Trim('[', ']'), out var address) && IPAddress.IsLoopback(address));

    public static async Task WriteJsonAsync(HttpListenerResponse response, int status, JsonNode? body)
    {
        var bytes = Encoding.UTF8.GetBytes(Json.Serialize(body));
        response.StatusCode = status;
        response.ContentType = "application/json; charset=utf-8";
        response.ContentLength64 = bytes.Length;
        await response.OutputStream.WriteAsync(bytes).ConfigureAwait(false);
    }

    public static async Task WriteBytesAsync(HttpListenerResponse response, int status, string contentType, byte[] bytes, string? cacheControl = "no-cache")
    {
        response.StatusCode = status;
        response.ContentType = contentType;
        if (cacheControl is not null)
            response.Headers["Cache-Control"] = cacheControl;
        response.ContentLength64 = bytes.Length;
        await response.OutputStream.WriteAsync(bytes).ConfigureAwait(false);
    }

    private static async Task TryWriteAsync(HttpListenerResponse response, int status, JsonNode body)
    {
        try
        {
            await WriteJsonAsync(response, status, body).ConfigureAwait(false);
        }
        catch (Exception)
        {
            // 已经开始写 SSE，或连接已断。
        }
    }

    /// <summary>共享密钥：环境变量 <c>ECHO_SHARE_KEY</c> 优先，否则数据目录里的 extension-key.txt（没有就生成）。</summary>
    public static string LoadShareKey(string file)
    {
        var fromEnv = Environment.GetEnvironmentVariable("ECHO_SHARE_KEY")?.Trim();
        if (!string.IsNullOrEmpty(fromEnv))
            return Validate(fromEnv);
        if (File.Exists(file))
            return Validate(File.ReadAllText(file).Trim());
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(file)!);
        var key = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32)).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        File.WriteAllText(file, key + "\n");
        return key;
    }

    internal static string Validate(string key)
    {
        if (key.Length is < 32 or > 256 || key.Any(c => c < 33 || c > 126))
            throw new InvalidOperationException("共享密钥需要 32～256 个 ASCII 非空白字符");
        return key;
    }
}
