using System.Globalization;
using HistoryVulcan.Core.Commands;

namespace Echo;

/// <summary>
/// 模块的运行状态，页面表格从这里取行。示范「指令改状态 → 通知 Aurora 刷新表格」这条回路。
/// </summary>
internal sealed class EchoState(ICommandBus? bus)
{
    private readonly object _gate = new();
    private readonly List<DateTime> _pings = [];

    public int Count
    {
        get
        {
            lock (_gate)
                return _pings.Count;
        }
    }

    public async Task<string> PingAsync()
    {
        int count;
        lock (_gate)
        {
            _pings.Add(DateTime.Now);
            count = _pings.Count;
        }

        await RefreshPageAsync().ConfigureAwait(false);
        return $"你好，这是第 {count} 次招呼。";
    }

    /// <summary>表格的行：最近的在上。</summary>
    public IReadOnlyList<IReadOnlyDictionary<string, string>> Rows()
    {
        lock (_gate)
        {
            return _pings
                .Select((time, index) => (IReadOnlyDictionary<string, string>)new Dictionary<string, string>
                {
                    ["id"] = (index + 1).ToString(CultureInfo.InvariantCulture),
                    ["index"] = (index + 1).ToString(CultureInfo.InvariantCulture),
                    ["time"] = time.ToString("HH:mm:ss", CultureInfo.InvariantCulture),
                })
                .Reverse()
                .ToList();
        }
    }

    /// <summary>页面没开着或没装 Aurora 时刷不到，不影响指令结果。</summary>
    private async Task RefreshPageAsync()
    {
        if (bus is null)
            return;
        try
        {
            await bus.ExecuteAsync(
                $"aurora.ui.refreshdata node={EchoPage.HistoryTableId}",
                // 来源由宿主盖章（module:Echo），模块不自填。
                "").ConfigureAwait(false);
        }
        catch (Exception)
        {
        }
    }
}
