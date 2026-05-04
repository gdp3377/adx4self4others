# AdXray Downloader - One-Click Update
# Downloads latest files from public GitHub repo (no token needed)

$repo   = "gdp3377/adx4self4others"
$branch = "main"
$skip   = @("config.json", "db_snapshot.json", ".gitignore", "README.md")

$baseRaw = "https://raw.githubusercontent.com/$repo/$branch"
$apiUrl  = "https://api.github.com/repos/$repo/contents?ref=$branch"

Write-Host "Checking for updates..." -ForegroundColor Cyan

# ── Fetch file list ──
try {
    $resp = Invoke-RestMethod -Uri $apiUrl -UseBasicParsing -ErrorAction Stop
} catch {
    Write-Host "Network error: $_" -ForegroundColor Red
    Write-Host "Please check your internet connection." -ForegroundColor Yellow
    exit 1
}

$files = $resp | Where-Object { $_.type -eq "file" -and $skip -notcontains $_.name }
$total = $files.Count
Write-Host "Found $total files.`n"

# ── Download each file ──
$ok = 0; $fail = 0; $i = 0
foreach ($f in $files) {
    $i++
    Write-Host "  [$i/$total] $($f.name) " -NoNewline
    try {
        $url = "$baseRaw/$($f.name)"
        Invoke-WebRequest -Uri $url -OutFile $f.name -UseBasicParsing -ErrorAction Stop
        Write-Host "OK" -ForegroundColor Green
        $ok++
    } catch {
        Write-Host "FAIL: $_" -ForegroundColor Red
        $fail++
    }
}

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  Update complete: $ok success, $fail failed" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
