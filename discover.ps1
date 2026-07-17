#!/usr/bin/env pwsh
param([int]$Port=9892,[string]$Key="sk-GkK1JMCH3Hl3rLaiJFKZ1vDeFlmJOY5a7Jt1o88PU4VZHnNn5hDe3CYQVOxJbGJT")

$authDir="$env:USERPROFILE\.local\share\opencode"
mkdir $authDir -Force|Out-Null
"@
$authJson = '{"opencode":{"type":"api","key":"' + $Key + '"},"opencode-go":{"type":"api","key":"' + $Key + '"}}'
$authJson | Set-Content "$authDir\auth.json" -Force

$script += @'

Write-Host "=== Installing ==="
npm install -g @opencode-ai/cli 2>&1 | Select-Object -Last 3

Write-Host "`n=== Starting server ==="
$serverJob = Start-Job -ScriptBlock { param($p) lildax serve --port $p } -ArgumentList $Port
Start-Sleep -Seconds 8

Write-Host "`n=== Probing endpoints ==="
$paths = @("/","/health","/v1","/openapi.json","/sse","/messages","/v1/chat/completions","/v1/models")
$methods = @("GET","GET","GET","GET","GET","GET","POST","GET")
for ($i=0; $i -lt $paths.Length; $i++) {
    $p = $paths[$i]; $m = $methods[$i]
    $url = "http://localhost:$Port$p"
    $j = Start-Job -ScriptBlock {
        param($u,$method)
        if ($method -eq "POST") {
            $r = curl.exe -s -w "HTTP:%{http_code}" --max-time 8 -X POST $u -H "Content-Type: application/json" -d '{"model":"opencode-go","messages":[{"role":"user","content":"ping"}]}' 2>&1
        } else {
            $r = curl.exe -s -w "HTTP:%{http_code}" --max-time 8 $u 2>&1
        }
        $r
    } -ArgumentList $url, $m
    $r = $j | Wait-Job -Timeout 10 | Receive-Job
    if (-not $r) { Stop-Job $j -ea 0; Remove-Job $j -Force -ea 0; $r="TIMEOUT" }
    Write-Host "  $($p.PadRight(25)) $r"
}

Write-Host "`n=== lildax api ==="
$j = Start-Job { lildax api GET / }
$r = $j | Wait-Job -Timeout 8 | Receive-Job
if (-not $r) { Stop-Job $j -ea 0; Remove-Job $j -Force -ea 0; $r="TIMEOUT" }
Write-Host "  $r"

Write-Host "`n=== Cleanup ==="
Stop-Job $serverJob -ea 0; Remove-Job $serverJob -Force -ea 0
