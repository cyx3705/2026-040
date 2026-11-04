namespace Echo.App;

/// <summary>
/// 推理入场控制：普通请求并发进入；换模型 / 改并发数要等现有请求全部结束，期间新请求排在后面。
/// 另有一个序列槽：对话、预测、伴读、输入翻译、起标题一次只跑一个（模型只有一份，A 不被别的请求顶掉）。
/// </summary>
internal sealed class EngineGate
{
    private readonly SemaphoreSlim _switching = new(1, 1);
    private readonly object _gate = new();
    private int _users;
    private TaskCompletionSource _idle = Completed();
    private SemaphoreSlim _requests = new(1, 1);

    public SemaphoreSlim Slot { get; } = new(1, 1);

    /// <summary>每次 HTTP 推理调用的并发上限（1 或 2）；不改服务端槽数。</summary>
    public int Parallel { get; private set; } = 1;

    private static TaskCompletionSource Completed()
    {
        var done = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        done.SetResult();
        return done;
    }

    /// <summary>以普通请求身份进入；Dispose 离开。</summary>
    public async Task<IDisposable> UseAsync(CancellationToken cancellation)
    {
        await _switching.WaitAsync(cancellation).ConfigureAwait(false);
        try
        {
            lock (_gate)
            {
                if (_users++ == 0)
                    _idle = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            }
        }
        finally
        {
            _switching.Release();
        }

        return new Lease(() =>
        {
            TaskCompletionSource? idle = null;
            lock (_gate)
            {
                if (--_users == 0)
                    idle = _idle;
            }

            idle?.TrySetResult();
        });
    }

    /// <summary>独占：挡住新请求并等现有请求结束。</summary>
    public async Task<IDisposable> ExclusiveAsync(CancellationToken cancellation)
    {
        await _switching.WaitAsync(cancellation).ConfigureAwait(false);
        try
        {
            Task idle;
            lock (_gate)
                idle = _idle.Task;
            await idle.WaitAsync(cancellation).ConfigureAwait(false);
        }
        catch
        {
            _switching.Release();
            throw;
        }

        return new Lease(() => _switching.Release());
    }

    /// <summary>占一个推理并发名额。</summary>
    public async Task<IDisposable> RequestAsync(CancellationToken cancellation)
    {
        var requests = _requests;
        await requests.WaitAsync(cancellation).ConfigureAwait(false);
        return new Lease(() => requests.Release());
    }

    public void SetParallel(int parallel)
    {
        if (parallel is not (1 or 2))
            throw new ArgumentOutOfRangeException(nameof(parallel), "并发数只支持 1 或 2");
        Parallel = parallel;
        _requests = new SemaphoreSlim(parallel, parallel);
    }

    private sealed class Lease(Action release) : IDisposable
    {
        private int _done;

        public void Dispose()
        {
            if (Interlocked.Exchange(ref _done, 1) == 0)
                release();
        }
    }
}
