# REPORT-001: build the editable manuscript, PDF, and standalone TeX archive.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# pdfLaTeX matches arXiv's TeX processing; XeLaTeX/LuaLaTeX sources are not accepted there.
$engine = Get-Command pdflatex -ErrorAction Stop
$scratch = Join-Path $PSScriptRoot 'tmp/pdfs'
$output = Join-Path $PSScriptRoot 'output/pdf'
New-Item -ItemType Directory -Path $scratch, $output -Force | Out-Null
Push-Location $PSScriptRoot
try {
    for ($pass = 1; $pass -le 2; $pass++) {
        $diagnostics = & $engine.Source '-interaction=nonstopmode' '-halt-on-error' '-file-line-error' "-output-directory=$scratch" '-jobname=echo-technical-report' 'main.tex' 2>&1
        if ($LASTEXITCODE -ne 0) {
            $diagnostics | Write-Output
            throw "pdfLaTeX pass $pass failed; inspect tmp/pdfs/echo-technical-report.log"
        }
    }
    $log = Get-Content (Join-Path $scratch 'echo-technical-report.log') -Raw
    if ($log -match 'Overfull \\[hv]box|Missing character:|undefined references|Citation .+ undefined') {
        throw 'PDF built with layout, font, or reference errors; inspect the compilation log.'
    }
    Copy-Item -LiteralPath (Join-Path $scratch 'echo-technical-report.pdf') -Destination $output -Force
    Compress-Archive -LiteralPath (Join-Path $PSScriptRoot 'main.tex') -DestinationPath (Join-Path $output 'echo-technical-report-source.zip') -Force
    Write-Output 'REPORT-001: PDF and standalone pdfLaTeX (arXiv) source archive generated in b-Office/technical-report/output/pdf.'
    Write-Output 'Render and inspect all PDF pages before delivery; compilation alone is not visual acceptance.'
}
finally {
    Pop-Location
}
