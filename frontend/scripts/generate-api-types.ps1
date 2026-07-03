<#
.SYNOPSIS
    Generates TypeScript API types from the backend's OpenAPI schema.
.DESCRIPTION
    Imports the FastAPI backend, dumps its OpenAPI schema to JSON, then runs
    openapi-typescript to produce frontend/src/lib/api/schema.ts.
    Run from the frontend/ directory via: npm run generate:api
#>

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path "$ScriptDir\..\.."
$BackendDir = Join-Path $RepoRoot "backend"
$OutputFile = Resolve-Path "$ScriptDir\..\src\lib\api\schema.ts"

# Temp files
$PyScript = Join-Path $env:TEMP "gen_openapi_$([System.Guid]::NewGuid().ToString('N')).py"
$TempSchema = Join-Path $env:TEMP "modulo_openapi_$([System.Guid]::NewGuid().ToString('N')).json"

Write-Output "=== Generating OpenAPI schema from backend..."

# Write the Python generator script (avoid PowerShell interpolation issues)
$PyContent = @'
import os, json, sys
sys.tracebacklimit = 0
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///TEMPLATE_DB"
os.environ["SECRET_KEY"] = "TEMPLATE_SECRET"
os.environ["FERNET_KEY"] = "TEMPLATE_FERNET"
os.environ["MODULO_CSRF_ENABLED"] = "false"
from modulo.api.main import app
with open(r"TEMPLATE_SCHEMA", "w", encoding="utf-8") as f:
    json.dump(app.openapi(), f, ensure_ascii=False)
print(f"Schema: {os.path.getsize(r'TEMPLATE_SCHEMA')} bytes")
'@
$PyContent = $PyContent.Replace("TEMPLATE_DB", ((Join-Path $env:TEMP "modulo-gen-test.db") -replace "\\", "/"))
$PyContent = $PyContent.Replace("TEMPLATE_SECRET", ("a" * 32))
$PyContent = $PyContent.Replace("TEMPLATE_FERNET", ("b" * 32))
$PyContent = $PyContent.Replace("TEMPLATE_SCHEMA", ($TempSchema -replace "\\", "/"))
[System.IO.File]::WriteAllText($PyScript, $PyContent, [System.Text.Encoding]::UTF8)

$prevDb = $env:DATABASE_URL
$prevSecret = $env:SECRET_KEY
$prevFernet = $env:FERNET_KEY
$prevCsrf = $env:MODULO_CSRF_ENABLED
try {
    $env:DATABASE_URL = "sqlite+aiosqlite:///$env:TEMP\modulo-gen-test.db"
    $env:SECRET_KEY = "a" * 32
    $env:FERNET_KEY = "b" * 32
    $env:MODULO_CSRF_ENABLED = "false"

    Push-Location $BackendDir
    python $PyScript
    if ($LASTEXITCODE -ne 0) { throw "Backend schema generation failed" }
} finally {
    $env:DATABASE_URL = $prevDb
    $env:SECRET_KEY = $prevSecret
    $env:FERNET_KEY = $prevFernet
    $env:MODULO_CSRF_ENABLED = $prevCsrf
    Pop-Location
}

if (-not (Test-Path $TempSchema)) { throw "Schema file was not created" }

Write-Output "=== Generating TypeScript types with openapi-typescript..."

Push-Location "$ScriptDir\.."
try {
    npx --yes openapi-typescript "$TempSchema" --output "$OutputFile"
    if ($LASTEXITCODE -ne 0) { throw "openapi-typescript failed" }
} finally {
    Pop-Location
}

Remove-Item $PyScript -Force -ErrorAction SilentlyContinue
Remove-Item $TempSchema -Force -ErrorAction SilentlyContinue
Remove-Item "$env:TEMP\modulo-gen-test.db" -Force -ErrorAction SilentlyContinue

Write-Output "=== Done: $OutputFile"
