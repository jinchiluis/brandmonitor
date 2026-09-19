[CmdletBinding()]
param(
    [string]$HistoryPath
)

$ErrorActionPreference = 'Stop'
if (-not $HistoryPath) {
    $HistoryPath = Join-Path $PSScriptRoot 'data\ip-history.csv'
}
$historyDirectory = Split-Path -Parent $HistoryPath
if ($historyDirectory -and -not (Test-Path -LiteralPath $historyDirectory)) {
    New-Item -ItemType Directory -Path $historyDirectory -Force | Out-Null
}
$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'
$ipv4 = ''
$status = 'ok'

try {
    $ipv4 = ([string](Invoke-RestMethod `
        -Uri 'https://api.ipify.org' `
        -TimeoutSec 15 `
        -UseBasicParsing)).Trim()

    $parsedAddress = $null
    if (-not [System.Net.IPAddress]::TryParse($ipv4, [ref]$parsedAddress) -or
        $parsedAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw "ipify returned an invalid IPv4 address"
    }
}
catch {
    $status = 'error: ' + (($_.Exception.Message -replace '[\r\n,]+', ' ').Trim())
}

$row = [pscustomobject]@{
    timestamp = $timestamp
    ipv4     = $ipv4
    status   = $status
}

if (Test-Path -LiteralPath $HistoryPath) {
    $row | Export-Csv -LiteralPath $HistoryPath -NoTypeInformation -Append -Encoding UTF8
}
else {
    $row | Export-Csv -LiteralPath $HistoryPath -NoTypeInformation -Encoding UTF8
}

if ($status -ne 'ok') {
    Write-Error $status
    exit 1
}

Write-Output "$timestamp $ipv4"
