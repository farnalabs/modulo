<#
.SYNOPSIS
  Checks product map graph integrity. Every ref resolves, every BDD exists,
  every PRD ref matches a section, every node has required fields.
  Exit code: 0 = clean, 1 = issues found.
#>
param([switch]$Fix,[switch]$CI)
$ErrorActionPreference="Stop"
$repoRoot=Resolve-Path (Join-Path $PSScriptRoot "..")
$productMap=Join-Path $repoRoot "docs/product-map"
$prdFile=Join-Path $repoRoot "docs/prd.md"
$bddRoot=Join-Path $repoRoot "backend/tests/bdd/features"
$issues=@()
. (Join-Path $PSScriptRoot "product-map-metadata.ps1")

# 1. Validate frontmatter
$entries=@()
Get-ChildItem -Recurse -Filter "*.md" -LiteralPath $productMap|Where-Object{$_.Name-ne"_index.md"}|ForEach-Object{
  $c=Get-Content -Raw -Encoding UTF8 -LiteralPath $_.FullName
  if($c -notmatch '(?s)^---[\r\n]+(.+?)[\r\n]+---'){$issues+="FILE|$($_.Name)|missing frontmatter";return}
  if($c-match'<<<<<<<|=======|>>>>>>>'){$issues+="CONFLICT|$($_.Name)|file contains unresolved merge conflict markers"}
  $fm=$Matches[1]
  $id=if($fm-match'(?m)^id:\s*(\S+)'){$Matches[1]}else{$null}
  $prdRefs=@(Get-ProductMapPrdReferences -Frontmatter $fm)
  $prd=if($prdRefs.Count -gt 0){$prdRefs -join ', '}else{$null}
  $bdd=@();if($fm-match'(?m)^bdd:\s*(.+?)[\r\n]'){$bList=$Matches[1].Trim();if($bList-match'^\['){$bdd=$bList-replace'[\[\]" ]',''-split','}};if($fm-match'(?m)^bdd:\s*\n((?:\s+- .+\n?)+)'){$bBlock=$Matches[1]-split'\n'|ForEach-Object{$_-replace'^\s*-\s*',''-replace'"',''-replace"'",''-replace'#.*',''.Trim()}|Where-Object{$_};if($bBlock){$bdd=@($bdd)+$bBlock}}
  $dep=@();if($fm-match'(?m)^depends-on:\s*\[(.*?)\]'){$dep=$Matches[1]-replace' ',''-split','};if($fm-match'(?m)^depends-on:\s*\n((?:\s+- .+\n?)+)'){$depBlock=$Matches[1]-split'\n'|ForEach-Object{$_-replace'^\s*-\s*',''-replace'"',''-replace"'",''-replace'#.*',''.Trim()}|Where-Object{$_};$dep=@($dep+$depBlock)|Where-Object{$_}}
  $codePaths=@();if($fm-match'(?m)^code:\s*\n((?:\s+- .+\n?)+)'){$lines=$Matches[1]-split'\n'|ForEach-Object{$_-replace'^\s*-\s*',''-replace'"',''.Trim()}|Where-Object{$_};$codePaths=$lines}
  $entries+=@{id=$id;prd=$prd;bdd=$bdd;depends=$dep;codePaths=$codePaths;path=$_.FullName;name=$_.Name}
  if(-not$id){$issues+="NODE|$($_.Name)|missing id field"}
  if(-not$prd){$issues+="NODE|$($_.Name)|missing prd field"}
  if($fm-notmatch'(?m)^status:\s*(covered|partial|gap)'){$issues+="NODE|$($_.Name)|missing or invalid status"}
}

# 2. Validate BDD refs
foreach($e in $entries){foreach($b in $e.bdd){if(-not$b){continue};$r=Join-Path $repoRoot $b;if(-not(Test-Path -LiteralPath $r)){$issues+="BDD|$($e.id)|$b not found"}}}

# 3. Validate depends-on
$allIds=@{};$entries|ForEach-Object{if($_.id){$allIds[$_.id]=$_.path}}
foreach($e in $entries){foreach($d in $e.depends){if(-not$d){continue};if(-not$allIds.ContainsKey($d)){$issues+="REF|$($e.id)|depends-on '$d' not found in any product map entry"}}}

# 4. Validate PRD section refs
$prdMetadata=Get-ProductMapPrdSections -Lines (Get-Content -LiteralPath $prdFile)
$prdSections=$prdMetadata.Sections
foreach($e in $entries){if(-not$e.prd-or$e.prd-match'(?i)^N/A$'){continue};$refs=$e.prd-split','|ForEach-Object{$_.Trim().TrimStart('§')};foreach($r in $refs){if(-not$prdSections.ContainsKey($r)){$issues+="PRD|$($e.id)|section $r not found in prd.md"}}}

# 5. Validate code paths exist
foreach($e in $entries){$c2=Get-Content -Raw -Encoding UTF8 -LiteralPath $e.path;if($c2-match'^code:'){$cs=$c2-split'---'|Select-Object -Index 2;if($cs-match'code:\s*\n((?:\s+- .+\n?)+)'){$lines=$Matches[1]-split'\n'|ForEach-Object{$_-replace'^\s*-\s*',''-replace'"',''};foreach($line in $lines){if(-not$line.Trim()){continue};$r=Join-Path $repoRoot $line.Trim();if(-not(Test-Path -LiteralPath $r)){if(-not(Test-Path -LiteralPath "$r.py")-and-not(Test-Path -LiteralPath "$r.vue")-and-not(Test-Path -LiteralPath "$r.ts")){$issues+="CODE|$($e.id)|$line not found"}}}}}}

# 6. Fix _index.md
if($Fix){$idx=Join-Path $productMap "_index.md";$ic=Get-Content -Raw -Encoding UTF8 -LiteralPath $idx;$ni=@("## Index","");$grps=$entries|Group-Object{[System.IO.Path]::GetFileName((Split-Path -Parent $_.path))}|Sort-Object Name;$gl=@{core="Core Platform";auth="Auth and Security";teams="Teams";evals="Evals and Feedback";connectors="Connectors";pipelines="Pipelines";frontend="Frontend";observability="Observability";infra="Infrastructure";"model-backends"="Model Backends";variants="Run Variants"};foreach($g in $grps){$l=$gl[$g.Name];if(-not$l){$l=$g.Name};$ni+="### $l";foreach($e in $g.Group|Sort-Object id){$rp=$e.path.Replace($productMap,"").TrimStart("\").Replace("\","/");if($e.prd){$ni+="- [$($e.id)]($rp) => PRD $($e.prd)"}else{$ni+="- [$($e.id)]($rp)"}};$ni+=""};$h=$ic-replace'(?s)## Index.*','';$f=$ic-replace'(?s).*## Index.*?\n##','##';$nc=$h.TrimEnd()+"`r`n`r`n"+($ni-join"`r`n")+"`r`n`r`n"+$f;Set-Content -Encoding UTF8 -LiteralPath $idx -Value $nc;Write-Host "Updated _index.md" -ForegroundColor Green}


# 7. Coverage orphans and anchors
# Parse PRD section names
    $prdSectionNames = $prdMetadata.Names

    # A. PRD→Map coverage — spec sections (§6–§12) with zero product map references
    # Children are considered covered if their parent section has coverage
    $specSections = @{}
    $prdMetadata.CoverageSections.Keys | Where-Object {
        $base = if ($_ -match '^(\d+)') { [int]$Matches[1] } else { -1 }
        $base -ge 6 -and $base -le 12
    } | ForEach-Object { $specSections[$_] = $true }

    $prdCounts = @{}; $parentCoverage = @{}
    foreach ($s in $specSections.Keys) { $prdCounts[$s] = 0 }
    foreach ($e in $entries) {
        if (-not $e.prd -or $e.prd -match '(?i)^N/A$') { continue }
        $refs = $e.prd -split ',' | ForEach-Object { $_.Trim().TrimStart('§') }
        foreach ($r in $refs) {
            if ($prdCounts.ContainsKey($r)) { $prdCounts[$r]++ }
            $base = if ($r -match '^(\d+)(\.|$)') { $Matches[1] } else { $r }
            if ($specSections.ContainsKey($base)) { $parentCoverage[$base] = $true }
        }
    }
    foreach ($s in ($specSections.Keys | Sort-Object)) {
        if ($prdCounts[$s] -eq 0) {
            $base = if ($s -match '^(\d+)') { $Matches[1] } else { $s }
            if (-not $parentCoverage.ContainsKey($base)) {
                $name = if ($prdSectionNames[$s]) { " ($($prdSectionNames[$s]))" } else { "" }
                $issues += "COVERAGE|PRD $s${name} -- 0 product map entries reference this section"
            }
        }
    }

    # B. Route→Map orphan check — route modules not in any product map code path
    $routeDir = Join-Path $repoRoot "backend/src/modulo/api/routes"
    $routeFiles = Get-ChildItem -Name -Path "$routeDir/*.py" | Where-Object { $_ -ne "__init__.py" } | Sort-Object
    foreach ($rf in $routeFiles) {
        $found = $false
        foreach ($e in $entries) {
            foreach ($cp in $e.codePaths) {
                if ($cp -match [regex]::Escape($rf)) { $found = $true; break }
            }
            if ($found) { break }
        }
        if (-not $found) { $issues += "ORPHAN|$rf|route module not referenced by any product map entry" }
    }

    # C. Naughty-section check — entries anchored to non-spec sections (§13–§15)
    $nonSpecSections = @{}; foreach ($n in @("13", "14", "15")) { $nonSpecSections[$n] = $prdSectionNames[$n] }
    foreach ($e in $entries) {
        if (-not $e.prd -or $e.prd -match '(?i)^N/A$') { continue }
        $refs = $e.prd -split ',' | ForEach-Object { $_.Trim().TrimStart('§') }
        foreach ($r in $refs) {
            $base = if ($r -match '^(\d+)') { $Matches[1] } else { $r }
            if ($nonSpecSections.ContainsKey($base)) {
                $baseName = $nonSpecSections[$base]
                $issues += "ANCHOR|$($e.id)|prd $r is a non-spec section ($baseName) -- use a feature subsection as anchor"
            }
        }
    }

if($issues.Count-eq 0){if(-not$CI){Write-Host "Graph is clean - $($entries.Count) entries, all refs resolve." -ForegroundColor Green};exit 0}else{if(-not$CI){Write-Host "$($issues.Count) issues found:" -ForegroundColor Red;$issues|ForEach-Object{$p=$_-split'\|',3;$detail=if($p.Count-gt 2){"$($p[1]) -> $($p[2])"}else{$p[1]};Write-Host "  [$($p[0])] $detail" -ForegroundColor Yellow}}else{$issues|ForEach-Object{Write-Host $_}};exit 1}
