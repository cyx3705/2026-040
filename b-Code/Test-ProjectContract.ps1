[CmdletBinding()]
param(
    [switch]$Instantiation
)

# 宿主模块仓的只读项目合同检查（随 0000-002-ModuleReady 模板派生，派生后不必改本文件）。
#
# 模块身份不写死：从 b-Code/*/module.manifest.json 里找唯一的模块，其余名字全部由它推出。
# 检查的都是「宿主或 Aurora 那边只会静默失败」的事：版本三处一致、模块身份、页面 owner 与指令域、
# z-Publish 按字节入库；外加目录级别、失效链接、密钥与占位符。
#
# 本脚本只读，不提交、不推送、不发布、不改外部系统。

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$errors = [Collections.Generic.List[string]]::new()
$warnings = [Collections.Generic.List[string]]::new()
$generated = '\\(?:bin|obj|\.git|z-Publish)\\'

function Require-File([string]$RelativePath) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $RelativePath) -PathType Leaf)) {
        $errors.Add("缺少必需文件: $RelativePath")
    }
}

function Require-Directory([string]$RelativePath) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $RelativePath) -PathType Container)) {
        $errors.Add("缺少必需目录: $RelativePath")
    }
}

function Read-Text([string]$Path) {
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8
}

try {
    $manifest = Read-Text (Join-Path $repoRoot 'project.manifest.json') | ConvertFrom-Json
}
catch {
    throw "无法读取 project.manifest.json: $($_.Exception.Message)"
}
$isTemplate = [bool]$manifest.template.isTemplate

foreach ($field in @('id', 'name', 'title', 'status', 'version')) {
    if ([string]::IsNullOrWhiteSpace([string]$manifest.project.$field)) {
        $errors.Add("project.manifest.json 缺少 project.$field")
    }
}

Require-File 'README.md'
Require-File 'AGENTS.md'
Require-File 'Logo.png'
Require-File 'Directory.Build.props'
foreach ($property in $manifest.documents.PSObject.Properties) {
    Require-File $property.Value
}
foreach ($directory in @($manifest.paths.activeRoots)) {
    Require-Directory $directory
}

# 根目录可见文件夹只允许 a-/b-/z- 三个级别。
$allowedPrefixes = @($manifest.paths.allowedRootDirectoryPrefixes)
Get-ChildItem -LiteralPath $repoRoot -Directory |
    Where-Object { -not $_.Name.StartsWith('.') } |
    ForEach-Object {
        $name = $_.Name
        if (-not ($allowedPrefixes | Where-Object { $name.StartsWith($_) })) {
            $errors.Add("根目录出现无级别文件夹: $name")
        }
    }

# ---- 找模块：b-Code 下唯一一份 module.manifest.json ----
$moduleManifests = @(Get-ChildItem -LiteralPath (Join-Path $repoRoot 'b-Code') -Recurse -File -Filter 'module.manifest.json' |
    Where-Object { $_.FullName -notmatch $generated })
if ($moduleManifests.Count -ne 1) {
    $errors.Add("b-Code 下必须恰好有一份 module.manifest.json，实际 $($moduleManifests.Count) 份")
    $errors | ForEach-Object { Write-Error $_ -ErrorAction Continue }
    exit 1
}

$componentRoot = $moduleManifests[0].DirectoryName
$moduleManifest = Read-Text $moduleManifests[0].FullName | ConvertFrom-Json
$moduleName = [string]$moduleManifest.name
if ($moduleName -cnotmatch '^[A-Z][A-Za-z0-9]*$') {
    $errors.Add("模块名必须是首字母大写的字母数字，实际 $moduleName")
}
if ((Split-Path -Leaf $componentRoot) -ne $moduleName) {
    $errors.Add("模块目录名必须与模块名相同：b-Code/$moduleName/")
}
if ($moduleManifest.type -ne 'HistoryVulcan.Module') {
    $errors.Add('module.manifest.json 的 type 必须是 HistoryVulcan.Module')
}
if ($moduleManifest.artifact -ne "$moduleName.dll") {
    $errors.Add("module.manifest.json 的 artifact 必须是 $moduleName.dll")
}
if (-not $isTemplate -and $manifest.project.name -ne $moduleName) {
    $errors.Add("project.name 必须等于模块名 $moduleName")
}

# 版本三处一致。宿主对版本漂移的处理是静默跳过整个模块，只在日志留一行警告，
# 所以这条必须是构建前的硬失败，而不是发布后的现场排查。
$propsPath = Join-Path $componentRoot "${moduleName}Version.props"
$moduleVersion = $null
if (-not (Test-Path -LiteralPath $propsPath)) {
    $errors.Add("缺少版本真源 ${moduleName}Version.props")
}
else {
    $versionMatch = [regex]::Match((Read-Text $propsPath), "<${moduleName}Version>(?<v>[^<]+)</${moduleName}Version>")
    if (-not $versionMatch.Success) {
        $errors.Add("${moduleName}Version.props 必须声明 <${moduleName}Version>")
    }
    else {
        $moduleVersion = $versionMatch.Groups['v'].Value
        if ($manifest.project.version -ne $moduleVersion -or $moduleManifest.version -ne $moduleVersion) {
            $errors.Add("版本必须三处一致：project.manifest=$($manifest.project.version) props=$moduleVersion module.manifest=$($moduleManifest.version)")
        }
    }
}

$moduleInfoPath = Join-Path $componentRoot 'ModuleInfo.cs'
if (-not (Test-Path -LiteralPath $moduleInfoPath) -or
    (Read-Text $moduleInfoPath) -notmatch "ModuleName\s*=>\s*`"$moduleName`"") {
    $errors.Add("ModuleInfo.ModuleName 必须显式返回 $moduleName：缺省值是程序集名，同时也是指令域")
}

# 页面 owner 必须等于 "History" + 首字母大写的指令域。Aurora 对不上时整页静默拒收，
# 只在日志留一条 Warn——界面上表现为「模块装上了但页面不见了」。
$identityFiles = @(Get-ChildItem -LiteralPath $componentRoot -File -Filter '*Identity.cs')
if ($identityFiles.Count -ne 1) {
    $errors.Add('模块目录下必须恰好有一个 *Identity.cs（模块名、指令域与页面身份的唯一权威源）')
}
else {
    $identitySource = Read-Text $identityFiles[0].FullName
    $domainMatch = [regex]::Match($identitySource, 'Domain\s*=\s*"(?<d>[a-z][a-z0-9]*)"')
    $ownerMatch = [regex]::Match($identitySource, 'PageOwner\s*=\s*"(?<o>[^"]+)"')
    if (-not $domainMatch.Success -or -not $ownerMatch.Success) {
        $errors.Add("$($identityFiles[0].Name) 必须声明 Domain（小写字母数字）与 PageOwner 字面量")
    }
    else {
        $domain = $domainMatch.Groups['d'].Value
        $expectedOwner = 'History' + $domain.Substring(0, 1).ToUpperInvariant() + $domain.Substring(1)
        if ($ownerMatch.Groups['o'].Value -cne $expectedOwner) {
            $errors.Add("页面 owner 必须是 $expectedOwner（由指令域 $domain 推出），实际 $($ownerMatch.Groups['o'].Value)")
        }
    }
}

$sourceFiles = Get-ChildItem -LiteralPath (Join-Path $repoRoot 'b-Code') -Recurse -File -Filter '*.cs' |
    Where-Object { $_.FullName -notmatch $generated }
foreach ($source in $sourceFiles) {
    $content = Read-Text $source.FullName
    if ($content -match 'context\.Settings|context\.DataDirectory') {
        $errors.Add("模块不得读取已移除的 IModuleContext 成员: $($source.FullName)")
    }
    # 宿主用可卸载装载上下文装模块，其中的类型不能参与 COM 互操作。
    if ($content -match '\[ComImport') {
        $errors.Add("模块内不得定义 [ComImport] 类型（可卸载装载上下文不支持）；Interop 请装进 AssemblyLoadContext.Default: $($source.FullName)")
    }
    # 宿主统一契约（5.9.0 起，6.0.0 收口）：数据目录、运行方式由上下文给，总线只用「执行 / 安静执行 / 确认」，装载冒烟走 --probe。
    if ($content -match 'Assembly\(\)\.Location|\.Assembly\.Location') {
        $errors.Add("不得用 Assembly.Location 找随包文件（宿主从内存流装载，它是空的）；包目录用 context.Environment.PackageDirectory: $($source.FullName)")
    }
    if ($content -match 'SpecialFolder\.ApplicationData') {
        $errors.Add("不得自己拼 %AppData% 路径；数据目录用 context.Environment.DataDirectory: $($source.FullName)")
    }
    if ($content -match '\bBus\.(Registry|Validate|Executed|Confirmation|RemoteExecutor|ShouldUseRemoteCommand)\b') {
        $errors.Add("不得使用宿主总线的内部成员；只用 ExecuteAsync / InvokeAsync / RequestConfirmation，目录走 vulcan.command.* 指令: $($source.FullName)")
    }
    if ($content -match 'new\s+ModuleHost\s*\(|HistoryVulcan\.Services\.|Environment\.GetCommandLineArgs') {
        $errors.Add("不得引用宿主实现（ModuleHost / HistoryVulcan.Services）或读进程参数判断运行方式；装载冒烟用 HistoryVulcan.Cli.exe --probe，运行方式用 context.Environment.RunMode: $($source.FullName)")
    }
}

foreach ($projectFile in Get-ChildItem -LiteralPath $repoRoot -Recurse -File -Filter '*.csproj' |
    Where-Object { $_.FullName -notmatch $generated }) {
    $content = Read-Text $projectFile.FullName
    if ($content -match '<ProjectReference[^>]+(?:\.\.\\|\.\./)+\d{4}-') {
        $errors.Add("不得跨仓 ProjectReference: $($projectFile.FullName)")
    }
    # 宿主 5.9.0 统一契约：只引用契约程序集 HistoryVulcan.Core。
    if ($content -match '<Reference\s+Include="HistoryVulcan\.(Services|ServiceHost|Tests)"|InternalsVisibleTo\s+Include="HistoryVulcan\.') {
        $errors.Add("只许引用宿主契约程序集 HistoryVulcan.Core，不许引用 Services / ServiceHost，也不许向宿主程序集开放 internals: $($projectFile.FullName)")
    }
}

# 发布快照按字节入库。缺这条时 finish 热装会因 SHA256SUMS 不符失败。
if ((Read-Text (Join-Path $repoRoot '.gitattributes')) -notmatch '(?m)^z-Publish/\*\* binary\s*$') {
    $errors.Add('.gitattributes 必须包含 z-Publish/** binary')
}

# Windows PowerShell 5.1 按系统代码页读无 BOM 的脚本，中文注释与字符串会被读坏。
foreach ($script in Get-ChildItem -LiteralPath $repoRoot -Recurse -File -Filter '*.ps1' |
    Where-Object { $_.FullName -notmatch $generated }) {
    $head = @([IO.File]::ReadAllBytes($script.FullName) | Select-Object -First 3)
    if ($head.Count -lt 3 -or $head[0] -ne 0xEF -or $head[1] -ne 0xBB -or $head[2] -ne 0xBF) {
        $errors.Add("PowerShell 脚本必须带 UTF-8 BOM: $($script.FullName)")
    }
}

# 密钥绝不入库。判据是真实密钥的形状，不是维护一张豁免名单。
$secretPattern = 'sk-[0-9a-fA-F]{32,}'
foreach ($file in Get-ChildItem -LiteralPath $repoRoot -Recurse -File |
    Where-Object {
        $_.FullName -notmatch '\\(?:bin|obj|\.git)\\' -and
        $_.Extension -in @('.md', '.json', '.cs', '.props', '.csproj', '.ps1', '.txt', '.yml', '.yaml')
    }) {
    foreach ($match in [regex]::Matches((Read-Text $file.FullName), $secretPattern)) {
        $errors.Add("疑似真实密钥入库: $($file.FullName) → $($match.Value.Substring(0, 6))…")
    }
}

# 现行文档里的本地 Markdown 链接必须指向存在的文件。
# 不要写指向别的仓的相对链接：F 盘工作区里它们必然失效。
foreach ($document in Get-ChildItem -LiteralPath $repoRoot -Recurse -File -Filter '*.md' |
    Where-Object { $_.FullName -notmatch $generated }) {
    $content = Read-Text $document.FullName
    foreach ($match in [regex]::Matches($content, '\]\((?<target>[^)#:]+)(?:#[^)]*)?\)')) {
        $target = $match.Groups['target'].Value.Trim()
        $resolved = Join-Path (Split-Path -Parent $document.FullName) $target
        if (-not (Test-Path -LiteralPath $resolved)) {
            $errors.Add("失效的本地链接: $($document.FullName) → $target")
        }
    }
}

if ($Instantiation) {
    if ($isTemplate) {
        $errors.Add('实例化项目的 template.isTemplate 必须为 false')
    }
    $textFiles = Get-ChildItem -LiteralPath $repoRoot -Recurse -File |
        Where-Object { $_.Extension -in @('.md', '.json', '.props', '.csproj', '.cs', '.ps1') } |
        Where-Object { $_.FullName -notmatch $generated -and $_.Name -notin @('AGENTS.md', 'Test-ProjectContract.ps1') }
    foreach ($file in $textFiles) {
        $content = Read-Text $file.FullName
        if ($content -match '\{\{.+?\}\}') {
            $errors.Add("存在未实例化占位符: $($file.FullName)")
        }
        if ($content -match 'Exemplum') {
            $errors.Add("残留模板名 Exemplum（先运行 b-Code\New-ModuleFromTemplate.ps1）: $($file.FullName)")
        }
    }

    # 宿主 5.8.0 起不登记模块：发布描述在本仓 project.manifest.json 的 publish 节，缺了 submit 会拒收。
    $publish = (Read-Text (Join-Path $repoRoot 'project.manifest.json') | ConvertFrom-Json).publish
    if ($null -eq $publish) {
        $errors.Add('project.manifest.json 缺少 publish 节（宿主开发管线靠它打包与验证）')
    }
    else {
        foreach ($field in @('versionProps', 'versionProperty', 'sourceManifest')) {
            if ([string]::IsNullOrWhiteSpace($publish.$field)) { $errors.Add("publish.$field 不能为空") }
            elseif ($field -ne 'versionProperty' -and -not (Test-Path -LiteralPath (Join-Path $repoRoot $publish.$field))) {
                $errors.Add("publish.$field 指向的文件不存在: $($publish.$field)")
            }
        }
        if ($null -eq $publish.package -or [string]::IsNullOrWhiteSpace($publish.package.project)) {
            $errors.Add('publish.package.project 不能为空')
        }
        elseif (-not (Test-Path -LiteralPath (Join-Path $repoRoot $publish.package.project))) {
            $errors.Add("publish.package.project 指向的文件不存在: $($publish.package.project)")
        }
    }
}

$warnings | ForEach-Object { Write-Warning $_ }
if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Error $_ -ErrorAction Continue }
    exit 1
}

$mode = if ($isTemplate) { 'template' } else { 'module' }
Write-Output "$moduleName project contract: PASS ($mode; $moduleVersion)"
