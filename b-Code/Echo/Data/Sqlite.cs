using System.Runtime.InteropServices;
using System.Text;

namespace Echo.Data;

/// <summary>SQLite 报错。</summary>
internal sealed class SqliteException(string message) : Exception(message);

/// <summary>
/// 最小 SQLite 封装，直接调 Windows 自带的 <c>winsqlite3.dll</c>（System32）。
/// </summary>
/// <remarks>
/// 宿主从 nupkg 的字节装载模块、不解析包里的原生库，所以不能带 e_sqlite3；系统自带这份从 Windows 10 起就在。
/// 一个连接只给一个线程同时用：调用方自己加锁（<see cref="Gate"/>）。
/// </remarks>
internal sealed class SqliteConnection : IDisposable
{
    private const string Lib = "winsqlite3";
    private const int Ok = 0, Row = 100, Done = 101;
    private const int OpenReadOnly = 0x1, OpenReadWrite = 0x2, OpenCreate = 0x4, OpenUri = 0x40, OpenFullMutex = 0x10000;
    private static readonly IntPtr Transient = new(-1);

    private IntPtr _db;

    public SqliteConnection(string path, bool readOnly = false)
    {
        var flags = (readOnly ? OpenReadOnly : OpenReadWrite | OpenCreate) | OpenFullMutex | OpenUri;
        var rc = sqlite3_open_v2(Utf8(path), out _db, flags, IntPtr.Zero);
        if (rc != Ok)
        {
            var message = _db == IntPtr.Zero ? $"code {rc}" : Error();
            if (_db != IntPtr.Zero)
                sqlite3_close_v2(_db);
            _db = IntPtr.Zero;
            throw new SqliteException($"打不开数据库 {path}：{message}");
        }

        sqlite3_busy_timeout(_db, 5000);
    }

    /// <summary>调用方共用这把锁来串行使用本连接。</summary>
    public object Gate { get; } = new();

    public void Dispose()
    {
        if (_db != IntPtr.Zero)
        {
            sqlite3_close_v2(_db);
            _db = IntPtr.Zero;
        }
    }

    /// <summary>执行一段可能含多条语句的 SQL（无参数）。</summary>
    public void Script(string sql)
    {
        var rc = sqlite3_exec(_db, Utf8(sql), IntPtr.Zero, IntPtr.Zero, out var error);
        if (rc != Ok)
        {
            var message = error == IntPtr.Zero ? Error() : Marshal.PtrToStringUTF8(error);
            if (error != IntPtr.Zero)
                sqlite3_free(error);
            throw new SqliteException(message ?? $"code {rc}");
        }
    }

    /// <summary>执行一条语句，返回受影响行数。</summary>
    public int Execute(string sql, params object?[] args)
    {
        using var statement = Prepare(sql, args);
        statement.Step();
        return sqlite3_changes(_db);
    }

    public long LastInsertRowId => sqlite3_last_insert_rowid(_db);

    /// <summary>查询全部行；每行是按列序的值（long / double / string / byte[] / null）。</summary>
    public List<object?[]> Query(string sql, params object?[] args)
    {
        using var statement = Prepare(sql, args);
        var rows = new List<object?[]>();
        while (statement.Step())
            rows.Add(statement.Values());
        return rows;
    }

    public object?[]? QueryOne(string sql, params object?[] args)
    {
        using var statement = Prepare(sql, args);
        return statement.Step() ? statement.Values() : null;
    }

    private Statement Prepare(string sql, object?[] args)
    {
        var bytes = Utf8(sql);
        var rc = sqlite3_prepare_v2(_db, bytes, bytes.Length, out var handle, IntPtr.Zero);
        if (rc != Ok)
            throw new SqliteException($"{Error()}：{sql}");
        var statement = new Statement(this, handle);
        try
        {
            for (var i = 0; i < args.Length; i++)
                statement.Bind(i + 1, args[i]);
        }
        catch
        {
            statement.Dispose();
            throw;
        }

        return statement;
    }

    private string Error() => Marshal.PtrToStringUTF8(sqlite3_errmsg(_db)) ?? "未知错误";

    private static byte[] Utf8(string text)
    {
        var bytes = Encoding.UTF8.GetBytes(text);
        Array.Resize(ref bytes, bytes.Length + 1);
        return bytes;
    }

    private sealed class Statement(SqliteConnection owner, IntPtr handle) : IDisposable
    {
        public void Bind(int index, object? value)
        {
            var rc = value switch
            {
                null => sqlite3_bind_null(handle, index),
                bool b => sqlite3_bind_int64(handle, index, b ? 1 : 0),
                int i => sqlite3_bind_int64(handle, index, i),
                long l => sqlite3_bind_int64(handle, index, l),
                double d => sqlite3_bind_double(handle, index, d),
                string s => BindText(index, s),
                byte[] blob => sqlite3_bind_blob(handle, index, blob, blob.Length, Transient),
                _ => throw new SqliteException($"不支持的参数类型 {value.GetType().Name}"),
            };
            if (rc != Ok)
                throw new SqliteException(owner.Error());
        }

        private int BindText(int index, string value)
        {
            var bytes = Encoding.UTF8.GetBytes(value);
            return sqlite3_bind_text(handle, index, bytes, bytes.Length, Transient);
        }

        public bool Step()
        {
            var rc = sqlite3_step(handle);
            if (rc == Row)
                return true;
            if (rc == Done)
                return false;
            throw new SqliteException(owner.Error());
        }

        public object?[] Values()
        {
            var count = sqlite3_column_count(handle);
            var values = new object?[count];
            for (var i = 0; i < count; i++)
            {
                values[i] = sqlite3_column_type(handle, i) switch
                {
                    1 => sqlite3_column_int64(handle, i),
                    2 => sqlite3_column_double(handle, i),
                    3 => Text(i),
                    4 => Blob(i),
                    _ => null,
                };
            }

            return values;
        }

        private string Text(int column)
        {
            var pointer = sqlite3_column_text(handle, column);
            var length = sqlite3_column_bytes(handle, column);
            return pointer == IntPtr.Zero ? string.Empty : Marshal.PtrToStringUTF8(pointer, length);
        }

        private byte[] Blob(int column)
        {
            var pointer = sqlite3_column_blob(handle, column);
            var data = new byte[sqlite3_column_bytes(handle, column)];
            if (pointer != IntPtr.Zero && data.Length > 0)
                Marshal.Copy(pointer, data, 0, data.Length);
            return data;
        }

        public void Dispose() => sqlite3_finalize(handle);
    }

    [DllImport(Lib)] private static extern int sqlite3_open_v2(byte[] filename, out IntPtr db, int flags, IntPtr vfs);
    [DllImport(Lib)] private static extern int sqlite3_close_v2(IntPtr db);
    [DllImport(Lib)] private static extern int sqlite3_busy_timeout(IntPtr db, int ms);
    [DllImport(Lib)] private static extern int sqlite3_exec(IntPtr db, byte[] sql, IntPtr callback, IntPtr arg, out IntPtr error);
    [DllImport(Lib)] private static extern void sqlite3_free(IntPtr pointer);
    [DllImport(Lib)] private static extern IntPtr sqlite3_errmsg(IntPtr db);
    [DllImport(Lib)] private static extern int sqlite3_changes(IntPtr db);
    [DllImport(Lib)] private static extern long sqlite3_last_insert_rowid(IntPtr db);
    [DllImport(Lib)] private static extern int sqlite3_prepare_v2(IntPtr db, byte[] sql, int bytes, out IntPtr statement, IntPtr tail);
    [DllImport(Lib)] private static extern int sqlite3_bind_null(IntPtr statement, int index);
    [DllImport(Lib)] private static extern int sqlite3_bind_int64(IntPtr statement, int index, long value);
    [DllImport(Lib)] private static extern int sqlite3_bind_double(IntPtr statement, int index, double value);
    [DllImport(Lib)] private static extern int sqlite3_bind_text(IntPtr statement, int index, byte[] value, int bytes, IntPtr destructor);
    [DllImport(Lib)] private static extern int sqlite3_bind_blob(IntPtr statement, int index, byte[] value, int bytes, IntPtr destructor);
    [DllImport(Lib)] private static extern int sqlite3_step(IntPtr statement);
    [DllImport(Lib)] private static extern int sqlite3_finalize(IntPtr statement);
    [DllImport(Lib)] private static extern int sqlite3_column_count(IntPtr statement);
    [DllImport(Lib)] private static extern int sqlite3_column_type(IntPtr statement, int column);
    [DllImport(Lib)] private static extern long sqlite3_column_int64(IntPtr statement, int column);
    [DllImport(Lib)] private static extern double sqlite3_column_double(IntPtr statement, int column);
    [DllImport(Lib)] private static extern IntPtr sqlite3_column_text(IntPtr statement, int column);
    [DllImport(Lib)] private static extern IntPtr sqlite3_column_blob(IntPtr statement, int column);
    [DllImport(Lib)] private static extern int sqlite3_column_bytes(IntPtr statement, int column);
}
