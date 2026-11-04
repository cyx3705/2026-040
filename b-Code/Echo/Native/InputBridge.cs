using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace Echo.Native;

/// <summary>
/// INPUT-001：本机 Windows 中文输入桥（C++ <c>input_bridge.dll</c>，User32/IMM/GDI）的生命周期与归属适配。
/// </summary>
/// <remarks>
/// 源码随包嵌入（kernel/），第一次用到时解出并用 VS C++ Build Tools 编译进数据目录 <c>native/input-&lt;摘要&gt;</c>；
/// 摘要算法与原 Python 版一致（文件名 + 内容，按文件名排序），已有构建直接复用。
/// </remarks>
internal sealed class InputBridge : IDisposable
{
    private readonly object _gate = new();
    private readonly string _nativeRoot;
    private IntPtr _library;
    private IntPtr _runtime;
    private Api? _api;

    public InputBridge(string nativeRoot) => _nativeRoot = nativeRoot;

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate IntPtr ErrorFn();
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate int CreateFn(out IntPtr runtime);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate void DestroyFn(IntPtr runtime);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Unicode)] private delegate int AcquireFn(IntPtr runtime, string marker, out ulong owner);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate int OwnerFn(IntPtr runtime, ulong owner);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate uint RefreshFn(IntPtr runtime, ulong owner);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int BoundsFn(IntPtr runtime, ulong owner, double left, double top, double width, double height,
        double viewportWidth, double viewportHeight, double fontSize, uint color, uint background);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate int TextFn(IntPtr runtime, ulong owner, [Out] char[] buffer, uint length);

    private sealed record Api(ErrorFn Error, CreateFn Create, DestroyFn Destroy, AcquireFn Acquire, OwnerFn Confirm,
        BoundsFn Bounds, RefreshFn Refresh, TextFn Text, OwnerFn Release);

    private static readonly string[] Sources = ["Build-Kernel.ps1", "input_bridge.cpp", "input_keys.h", "input_tests.cpp"];

    /// <summary>嵌入的 C++ 源码（文件名 → 字节）。</summary>
    internal static SortedDictionary<string, byte[]> LoadSources()
    {
        var assembly = typeof(InputBridge).Assembly;
        var result = new SortedDictionary<string, byte[]>(StringComparer.Ordinal);
        foreach (var name in Sources)
        {
            using var stream = assembly.GetManifestResourceStream("kernel/" + name)
                               ?? throw new InvalidOperationException($"模块包缺少输入桥源码 {name}");
            using var buffer = new MemoryStream();
            stream.CopyTo(buffer);
            result[name] = buffer.ToArray();
        }

        return result;
    }

    internal static string BuildDirectory(string nativeRoot, SortedDictionary<string, byte[]> sources)
    {
        using var sha = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        foreach (var (name, bytes) in sources)
        {
            sha.AppendData(Encoding.UTF8.GetBytes(name));
            sha.AppendData(bytes);
        }

        return Path.Combine(nativeRoot, "input-" + Convert.ToHexString(sha.GetHashAndReset()).ToLowerInvariant()[..16]);
    }

    /// <summary>确保输入桥已编译，返回 DLL 路径。<paramref name="output"/> 收编译输出。</summary>
    public string EnsureBuilt(IProgress<string>? output = null)
    {
        var sources = LoadSources();
        var directory = BuildDirectory(_nativeRoot, sources);
        var dll = Path.Combine(directory, "input_bridge.dll");
        var marker = Path.Combine(directory, "complete");
        if (File.Exists(dll) && File.Exists(marker))
            return dll;
        Directory.CreateDirectory(directory);
        using var mutex = new Mutex(false, "Global\\EchoInputBridgeBuild");
        mutex.WaitOne();
        try
        {
            if (File.Exists(dll) && File.Exists(marker))
                return dll;
            var src = Path.Combine(directory, "src");
            Directory.CreateDirectory(src);
            foreach (var (name, bytes) in sources)
                File.WriteAllBytes(Path.Combine(src, name), bytes);
            var start = new ProcessStartInfo("powershell.exe")
            {
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            };
            foreach (var arg in new[] { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", Path.Combine(src, "Build-Kernel.ps1"), "-OutputDirectory", directory, "-InputOnly" })
                start.ArgumentList.Add(arg);
            using var process = Process.Start(start)!;
            var log = new StringBuilder();
            process.OutputDataReceived += (_, e) => { if (e.Data is not null) { log.AppendLine(e.Data); output?.Report(e.Data); } };
            process.ErrorDataReceived += (_, e) => { if (e.Data is not null) { log.AppendLine(e.Data); output?.Report(e.Data); } };
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            process.WaitForExit();
            if (process.ExitCode != 0)
            {
                var text = log.ToString();
                throw new InvalidOperationException("输入桥编译失败（需要 VS C++ Build Tools + Windows SDK，或设 ECHO_VCVARS64）：\n"
                                                    + (text.Length > 4000 ? text[^4000..] : text));
            }

            File.WriteAllText(marker, "1");
            return dll;
        }
        finally
        {
            mutex.ReleaseMutex();
        }
    }

    private Api Start()
    {
        if (_api is not null)
            return _api;
        var dll = EnsureBuilt();
        _library = NativeLibrary.Load(dll);
        T Fn<T>(string name) where T : Delegate => Marshal.GetDelegateForFunctionPointer<T>(NativeLibrary.GetExport(_library, name));
        var api = new Api(Fn<ErrorFn>("echo_input_error"), Fn<CreateFn>("echo_input_create"), Fn<DestroyFn>("echo_input_destroy"),
            Fn<AcquireFn>("echo_input_acquire"), Fn<OwnerFn>("echo_input_confirm"), Fn<BoundsFn>("echo_input_bounds"),
            Fn<RefreshFn>("echo_input_refresh"), Fn<TextFn>("echo_input_text"), Fn<OwnerFn>("echo_input_release"));
        if (api.Create(out _runtime) != 0)
            throw new InvalidOperationException(Error(api));
        return _api = api;
    }

    private static string Error(Api api) => Marshal.PtrToStringAnsi(api.Error()) ?? "input_error";

    private JsonObject Status(ulong owner, uint state)
    {
        var committed = "";
        if (_api is { } api && owner != 0)
        {
            var buffer = new char[65537];
            if (api.Text(_runtime, owner, buffer, (uint)buffer.Length) != 0)
                throw new InvalidOperationException(Error(api));
            var end = Array.IndexOf(buffer, '\0');
            committed = new string(buffer, 0, end < 0 ? buffer.Length : end);
        }

        return new JsonObject
        {
            ["owner"] = owner, ["active"] = (state & 1) != 0, ["ready"] = (state & 2) != 0, ["chinese"] = (state & 4) != 0,
            ["failed"] = (state & 8) != 0, ["verified"] = (state & 16) != 0, ["proxy"] = (state & 32) != 0, ["committed"] = committed,
        };
    }

    public JsonObject Acquire(string marker)
    {
        lock (_gate)
        {
            var api = Start();
            if (api.Acquire(_runtime, marker, out var owner) != 0)
                throw new InvalidOperationException(Error(api));
            return Status(owner, api.Refresh(_runtime, owner));
        }
    }

    public JsonObject Refresh(ulong owner)
    {
        lock (_gate)
            return Status(owner, _api is { } api ? api.Refresh(_runtime, owner) : 0);
    }

    public void Bounds(ulong owner, double[] values, uint color, uint background)
    {
        lock (_gate)
        {
            if (_api is not { } api)
                return;
            if (api.Bounds(_runtime, owner, values[0], values[1], values[2], values[3], values[4], values[5], values[6], color, background) != 0)
                throw new InvalidOperationException(Error(api));
        }
    }

    public JsonObject Confirm(ulong owner)
    {
        lock (_gate)
        {
            if (_api is not { } api)
                throw new InvalidOperationException("input_unavailable");
            if (api.Confirm(_runtime, owner) != 0)
                throw new InvalidOperationException(Error(api));
            return Status(owner, api.Refresh(_runtime, owner));
        }
    }

    public void Release(ulong owner)
    {
        lock (_gate)
        {
            if (_api is { } api && api.Release(_runtime, owner) != 0)
                throw new InvalidOperationException(Error(api));
        }
    }

    public void Dispose()
    {
        lock (_gate)
        {
            if (_api is { } api)
                api.Destroy(_runtime);
            _api = null;
            _runtime = IntPtr.Zero;
            if (_library != IntPtr.Zero)
                NativeLibrary.Free(_library);
            _library = IntPtr.Zero;
        }
    }
}
