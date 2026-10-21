[CmdletBinding()]
param(
    [switch]$Instantiation
)

# 项目合同的只读检查（派生项目原样继承本文件）。
#
# 原先本文件只是转发到 HistoryDiana 的 OneHistory.ModuleContract.ps1，那份共享规则已不在库中，
# 转发必然 exit 2。现在规则自带，且只检查对任何 OneHistory 项目都成立的事：manifest 身份、
# 入口文件、manifest 声明的文档与活动目录存在、根目录只有 a-/b-/z- 级别、现行文档的本地链接不断。
# 模块项目另有版本三处一致等规则，见模块模板 0000-002-ModuleReady。
#
# 派生项目需要差异时改 project.manifest.json，不要在这里加项目专属规则。
# 本脚本只读，不提交、不推送、不发布、不改外部系统。

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$errors = [Collections.Generic.List[string]]::new()

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

try {
    $manifest = Get-Content -LiteralPath (Join-Path $repoRoot 'project.manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    throw "无法读取 project.manifest.json: $($_.Exception.Message)"
}

$isTemplate = [bool]$manifest.template.isTemplate
foreach ($field in @('id', 'name', 'title', 'status')) {
    if ([string]::IsNullOrWhiteSpace([string]$manifest.project.$field)) {
        $errors.Add("project.manifest.json 缺少 project.$field")
    }
}
if ($Instantiation -and $isTemplate) {
    $errors.Add('派生项目的 template.isTemplate 必须为 false')
}

Require-File 'README.md'
Require-File 'AGENTS.md'
foreach ($property in $manifest.documents.PSObject.Properties) {
    Require-File $property.Value
}
foreach ($directory in @($manifest.paths.activeRoots)) {
    Require-Directory $directory
}

# 根目录可见文件夹只允许 manifest 声明的级别前缀（缺省 a-/b-/z-）。
$allowedPrefixes = @($manifest.paths.allowedRootDirectoryPrefixes)
if ($allowedPrefixes.Count -eq 0) { $allowedPrefixes = @('a-', 'b-', 'z-') }
foreach ($folder in Get-ChildItem -LiteralPath $repoRoot -Directory) {
    if ($folder.Name.StartsWith('.')) { continue }
    $matched = $false
    foreach ($prefix in $allowedPrefixes) {
        if ($folder.Name.StartsWith($prefix)) { $matched = $true }
    }
    if (-not $matched) {
        $errors.Add("根目录出现无级别文件夹: $($folder.Name)")
    }
}

# 现行文档里的本地 Markdown 链接必须指向存在的文件（history 只读归档与 z-* 发布区不查）。
$documents = Get-ChildItem -LiteralPath $repoRoot -Recurse -File -Filter '*.md' |
    Where-Object { $_.FullName -notmatch '\\(?:bin|obj|node_modules|history|\.git)\\' -and $_.FullName -notmatch '\\z-[^\\]*\\' }
foreach ($document in $documents) {
    $content = Get-Content -LiteralPath $document.FullName -Raw -Encoding UTF8
    foreach ($match in [regex]::Matches($content, '\]\((?<target>[^)#:]+)(?:#[^)]*)?\)')) {
        $target = $match.Groups['target'].Value.Trim()
        if ($target.Length -eq 0 -or $target.StartsWith('http') -or $target.StartsWith('mailto:')) { continue }
        $resolved = Join-Path (Split-Path -Parent $document.FullName) $target
        if (-not (Test-Path -LiteralPath $resolved)) {
            $errors.Add("失效的本地链接: $($document.FullName) → $target")
        }
    }
}

# 派生项目不得留下模板占位符 {{…}}；模板自身允许。
if ($Instantiation) {
    $textFiles = Get-ChildItem -LiteralPath $repoRoot -Recurse -File |
        Where-Object { $_.Extension -in @('.md', '.json', '.props', '.csproj') } |
        Where-Object { $_.FullName -notmatch '\\(?:bin|obj|history|\.git)\\' -and $_.FullName -notmatch '\\z-[^\\]*\\' }
    foreach ($file in $textFiles) {
        if ((Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8) -match '\{\{.+?\}\}') {
            $errors.Add("存在未实例化占位符: $($file.FullName)")
        }
    }
}

if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Error $_ }
    exit 1
}

$kind = if ($isTemplate) { 'template' } else { 'project' }
Write-Output "$($manifest.project.name) project contract: PASS ($kind)"
