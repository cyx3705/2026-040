namespace Echo;

/// <summary>
/// Echo 的两类数据位置。
/// </summary>
/// <remarks>
/// 用户自己的数据（学会的词、对话、单词表、设置）是长期资产，放仓内 <c>z-学习数据/user.db</c>、纳入 git；
/// 数据目录（默认 <c>%USERPROFILE%\.echo</c>，即 F:\ModuleServicesData\Echo 的联接，<c>ECHO_DATA_DIR</c> 可改）
/// 只放删了能重建的东西：ECDICT 词典库、原生输入桥构建、伴读上下文。
/// </remarks>
internal sealed record EchoPaths(string DataDirectory, string UserDatabase)
{
    /// <summary>项目在库里的目录名。</summary>
    public const string ProjectFolder = "2026-040-Echo";

    public string DictionaryDatabase => Path.Combine(DataDirectory, "dict", "ecdict.db");

    public string ReaderContextDirectory => Path.Combine(DataDirectory, "reader-context");

    public string NativeDirectory => Path.Combine(DataDirectory, "native");

    public string ShareKeyFile => Path.Combine(DataDirectory, "extension-key.txt");

    /// <summary>按环境变量与项目库根解析；<paramref name="libraryRoot"/> 取自 <c>vulcan.host.info</c>。</summary>
    public static EchoPaths Resolve(string? libraryRoot)
    {
        var data = Environment.GetEnvironmentVariable("ECHO_DATA_DIR");
        if (string.IsNullOrWhiteSpace(data))
            data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".echo");
        var user = Environment.GetEnvironmentVariable("ECHO_USER_DB");
        if (string.IsNullOrWhiteSpace(user))
        {
            if (string.IsNullOrWhiteSpace(libraryRoot))
                throw new InvalidOperationException("宿主没有给出项目库根（libraryRoot），找不到 z-学习数据/user.db；可设 ECHO_USER_DB 指定");
            user = Path.Combine(libraryRoot, ProjectFolder, "z-学习数据", "user.db");
        }

        return new EchoPaths(data, user);
    }
}
