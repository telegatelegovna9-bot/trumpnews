Set-Location "C:\Users\fames_rd\Desktop\trump mode"
$Output = @()

$Output += "=== GIT STATUS ==="
$Output += (git status 2>&1 | Out-String)

$Output += "`n=== GIT LOG ==="
$Output += (git log --oneline -3 2>&1 | Out-String)

$Output += "`n=== GIT ADD ==="
$Output += (git add . 2>&1 | Out-String)

$Output += "`n=== GIT COMMIT ==="
$Output += (git commit -m "v3: API interception via Playwright" 2>&1 | Out-String)

$Output += "`n=== GIT PUSH ==="
$Output += (git push origin main 2>&1 | Out-String)

$Output | Out-File -FilePath "C:\Users\fames_rd\Desktop\trump mode\git_result.txt" -Encoding UTF8
Write-Output "Done! Check git_result.txt"
