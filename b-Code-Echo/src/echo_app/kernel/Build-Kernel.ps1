param([Parameter(Mandatory=$true)][string]$OutputDirectory, [switch]$InputOnly)
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$out = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $out -Force | Out-Null

# Prefer registered Build Tools; also allow a retained, working VS toolchain.
$vcvars = $env:ECHO_VCVARS64
if (-not $vcvars) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
    if (Test-Path -LiteralPath $vswhere) {
        $vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if ($vs) { $vcvars = Join-Path $vs 'VC/Auxiliary/Build/vcvars64.bat' }
    }
}
if (-not $vcvars) {
    $vcvars = Get-ChildItem -Path "$env:ProgramFiles/Microsoft Visual Studio/*/*/VC/Auxiliary/Build/vcvars64.bat" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $vcvars -or -not (Test-Path -LiteralPath $vcvars)) {
    throw 'Install Visual Studio C++ Build Tools (x64 + Windows SDK), or set ECHO_VCVARS64 to vcvars64.bat.'
}
# Keep all compiler/linker outputs outside the source checkout. /MT needs no VC runtime installation.
$batchFile = Join-Path $out 'build.cmd'
$inferenceBuild = ''
if (-not $InputOnly) {
    $inferenceBuild = @"
cl /nologo /std:c++17 /O2 /EHsc /MT /utf-8 /W4 /LD "$source\echo_kernel.cpp" /Fe:echo_kernel.dll /link /INCREMENTAL:NO
if errorlevel 1 exit /b 1
cl /nologo /std:c++17 /O2 /EHsc /MT /utf-8 /W4 "$source\kernel_tests.cpp" /Fe:kernel_tests.exe /link /INCREMENTAL:NO
if errorlevel 1 exit /b 1
"@
}
$batchText = @"
@echo off
chcp 65001 >nul
call "$vcvars" >nul
if errorlevel 1 exit /b 1
cd /d "$out"
$inferenceBuild
cl /nologo /std:c++17 /O2 /EHsc /MT /utf-8 /W4 /LD "$source\input_bridge.cpp" /Fe:input_bridge.dll /link /INCREMENTAL:NO user32.lib imm32.lib gdi32.lib
if errorlevel 1 exit /b 1
cl /nologo /std:c++17 /O2 /EHsc /MT /utf-8 /W4 "$source\input_tests.cpp" /Fe:input_tests.exe /link /INCREMENTAL:NO user32.lib imm32.lib gdi32.lib
exit /b %errorlevel%
"@
[IO.File]::WriteAllText($batchFile, $batchText, (New-Object Text.UTF8Encoding($false)))
& $env:ComSpec /d /c $batchFile
if ($LASTEXITCODE -ne 0) { throw "C++ kernel build failed ($LASTEXITCODE)" }
