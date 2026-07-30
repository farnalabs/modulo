$content = [System.IO.File]::ReadAllText('frontend/src/manifest.yaml')
$content = $content -replace 'label: CORE', 'label: BUILD'
$content = $content -replace 'group_core', 'group_build'
[System.IO.File]::WriteAllText('frontend/src/manifest.yaml', $content)
