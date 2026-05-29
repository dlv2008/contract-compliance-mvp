param(
    [Parameter(Mandatory = $true)]
    [string]$BashScriptPath,

    [switch]$Sudo
)

$ErrorActionPreference = "Stop"

$keyPath = "C:\Users\dlv20\.ssh\id_ed25519"
$hostName = "www.trendbot.cn"
$port = 7956
$user = "dlv"
$sudoPassword = "guoxin.0110"

if (-not (Test-Path -LiteralPath $BashScriptPath)) {
    throw "Script not found: $BashScriptPath"
}

$scriptBytes = [System.IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $BashScriptPath))
$scriptBase64 = [Convert]::ToBase64String($scriptBytes)

if ($Sudo) {
    $remoteCommand = "printf '%s' '$scriptBase64' | base64 -d > /tmp/codex_remote.sh && chmod 700 /tmp/codex_remote.sh && printf '%s\n' '$sudoPassword' | sudo -S -p '' /bin/bash /tmp/codex_remote.sh"
} else {
    $remoteCommand = "printf '%s' '$scriptBase64' | base64 -d > /tmp/codex_remote.sh && chmod 700 /tmp/codex_remote.sh && /bin/bash /tmp/codex_remote.sh"
}

& ssh -i $keyPath -p $port -o StrictHostKeyChecking=accept-new "$user@$hostName" $remoteCommand
