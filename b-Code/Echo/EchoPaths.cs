namespace Echo;

/// <summary>
/// Echo 的两类数据位置。
/// </summary>
/// <remarks>
/// 用户自己的数据（学会的词、对话、单词表、设置）是长期资产，放仓内 <c>z-学习数据/user.db</c>、纳入 git。
/// 0.6.0 起它是 z 地址 <see cref="DefaultUserDatabase"/>，经 <c>diana.z.query</c> / <c>diana.z.exec</c> 访问，
/// Echo 自己不打开它：z 只在源码区机器上、从不迁移，Echo 迁到哪台机器都一样。
/// <c>ECHO_USER_DB</c> 可改成别的 z 地址，或本机绝对路径（只给测试排障）。
/// 数据目录（默认 <c>%USERPROFILE%\.echo</c>，即 F:\ModuleServicesData\Echo 的联接，<c>ECHO_DATA_DIR</c> 可改）
/// 只放删了能重建的东西：ECDICT 词典库、原生输入桥构建、伴读上下文。
/// </remarks>
internal sealed record EchoPaths(string DataDirectory, string UserDatabase)
{
    /// <summary>user.db 的 z 地址。</summary>
    public const string DefaultUserDatabase = "2026-040/z-学习数据/user.db";

    public string DictionaryDatabase => Path.Combine(DataDirectory, "dict", "ecdict.db");

    public string ReaderContextDirectory => Path.Combine(DataDirectory, "reader-context");

    public string NativeDirectory => Path.Combine(DataDirectory, "native");

    public string ShareKeyFile => Path.Combine(DataDirectory, "extension-key.txt");

    /// <summary>按环境变量解析；user.db 缺省是 z 地址，不再依赖项目库根。</summary>
    public static EchoPaths Resolve()
    {
        var data = Environment.GetEnvironmentVariable("ECHO_DATA_DIR");
        if (string.IsNullOrWhiteSpace(data))
            data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".echo");
        var user = Environment.GetEnvironmentVariable("ECHO_USER_DB");
        return new EchoPaths(data, string.IsNullOrWhiteSpace(user) ? DefaultUserDatabase : user.Trim());
    }
}
