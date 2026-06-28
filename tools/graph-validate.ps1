<#
.SYNOPSIS
  Checks product map graph integrity. Every ref resolves, every BDD exists,
  every PRD ref matches a section, every node has required fields.
  Exit code: 0 = clean, 1 = issues found.
#>
param([switch]$Fix,[switch]$CI)
$ErrorActionPreference="Stop"
$repoRoot=Resolve-Path (Join-Path $PSScriptRoot "..")
$productMap=Join-Path $repoRoot "docs\product-map"
$prdFile=Join-Path $repoRoot "docs\prd.md"
$bddRoot=Join-Path $repoRoot "backend\tests\bdd\features"
$issues=@()

# 1. Validate frontmatter
$entries=@()
Get-ChildItem -Recurse -Filter "*.md" -LiteralPath $productMap|Where-Object{$_.Name-ne"_index.md"}|ForEach-Object{
  $c=Get-Content -Raw -Encoding UTF8 -LiteralPath $_.FullName
  if($c -notmatch '(?s)^---[\r\n]+(.+?)[\r\n]+---'){$issues+="FILE|$($_.Name)|missing frontmatter";return}
  $fm=$Matches[1]
  $id=if($fm-match'(?m)^id:\s*(\S+)'){$Matches[1]}else{$null}
  $prd=if($fm-match'(?m)^prd:\s*(.+?)[\r\n]'){$Matches[1]}else{$null}
  $bdd=@();if($fm-match'(?m)^bdd:\s*(.+?)[\r\n]'){$bList=$Matches[1].Trim();if($bList-match'^\['){$bdd=$bList-replace'[\[\]" ]',''-split','}}
  $dep=@();if($fm-match'(?m)^depends-on:\s*\[(.*?)\]'){$dep=$Matches[1]-replace' ',''-split','}
  $entries+=@{id=$id;prd=$prd;bdd=$bdd;depends=$dep;path=$_.FullName;name=$_.Name}
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
$prdSections=@{}
if(Test-Path -LiteralPath $prdFile){Get-Content -LiteralPath $prdFile|ForEach-Object{if($_ -match '^### ((\d+\.\d+))'){$prdSections[$Matches[1]]=$true}elseif($_ -match '^## (\d+)\.'){$prdSections[$Matches[1]]=$true}}}
foreach($e in $entries){if(-not$e.prd){continue};$refs=$e.prd-split','|ForEach-Object{$_.Trim()};foreach($r in $refs){if(-not$prdSections.ContainsKey($r)){$issues+="PRD|$($e.id)|section $r not found in prd.md"}}}

# 5. Validate code paths exist
foreach($e in $entries){$c2=Get-Content -Raw -Encoding UTF8 -LiteralPath $e.path;if($c2-match'^code:'){$cs=$c2-split'---'|Select-Object -Index 2;if($cs-match'code:\s*\n((?:\s+- .+\n?)+)'){$lines=$Matches[1]-split'\n'|ForEach-Object{$_-replace'^\s*-\s*',''-replace'"',''};foreach($line in $lines){if(-not$line.Trim()){continue};$r=Join-Path $repoRoot $line.Trim();if(-not(Test-Path -LiteralPath $r)){if(-not(Test-Path -LiteralPath "$r.py")-and-not(Test-Path -LiteralPath "$r.vue")-and-not(Test-Path -LiteralPath "$r.ts")){$issues+="CODE|$($e.id)|$line not found"}}}}}}

# 6. Fix _index.md
if($Fix){$idx=Join-Path $productMap"_index.md";$ic=Get-Content -Raw -Encoding UTF8 -LiteralPath $idx;$ni=@("## Index","");$grps=$entries|Group-Object{[System.IO.Path]::GetFileName((Split-Path -Parent $_.path))}|Sort-Object Name;$gl=@{core="Core Platform";auth="Auth and Security";teams="Teams";evals="Evals and Feedback";connectors="Connectors";pipelines="Pipelines";frontend="Frontend";observability="Observability";"model-backends"="Model Backends";variants="Run Variants"};foreach($g in $grps){$l=$gl[$g.Name];if(-not$l){$l=$g.Name};$ni+="### $l";foreach($e in $g.Group|Sort-Object id){$rp=$e.path.Replace($productMap,"").TrimStart("\").Replace("\","/");if($e.prd){$ni+="- [$($e.id)]($rp) => PRD $($e.prd)"}else{$ni+="- [$($e.id)]($rp)"}};$ni+=""};$h=$ic-replace'(?s)## Index.*','';$f=$ic-replace'(?s).*## Index.*?\n##','##';$nc=$h.TrimEnd()+"``n``n"+($ni-join"``n")+"``n``n"+$f;Set-Content -Encoding UTF8 -LiteralPath $idx -Value $nc;Write-Host "Updated _index.md" -ForegroundColor Green}

if($issues.Count-eq 0){if(-not$CI){Write-Host "Graph is clean - $($entries.Count) entries, all refs resolve." -ForegroundColor Green};exit 0}else{if(-not$CI){Write-Host "$($issues.Count) issues found:" -ForegroundColor Red;$issues|ForEach-Object{$p=$_-split'\|';Write-Host "  [$($p[0])] $($p[1]) -> $($p[2])" -ForegroundColor Yellow}}else{$issues|ForEach-Object{Write-Host $_}};exit 1}
