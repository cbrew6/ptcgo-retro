# Reports whether this machine holds a PTCGO client cache worth keeping.
#
# The installer only ships a BASELINE: card data through SM4, a localization
# table through SM6, and four set icons. Everything after that arrived over
# the wire on first login and was cached locally. So a machine that actually
# played the game holds content that no longer exists anywhere else.
#
# Everything of value is under the client's runtime folder:
#
#   archetypes\            one file per card, full definitions - attack costs,
#                          damage, game text, ability IDs. A player who was
#                          active in 2023 has every card PTCGO ever shipped.
#   bundleCache\           the downloaded art bundles: card faces, foil masks,
#                          set symbols, product images.
#   LocalizationDB-*.db    card names, attack text, set names.
#   AttributeDB.db         attribute definitions.
#
# Run this and send the output. Nothing here reads or reports personal data:
# no account file, no config, no logs.

$ErrorActionPreference = "SilentlyContinue"

Write-Host "Looking for a PTCGO client cache..." -ForegroundColor Cyan
Write-Host ""

# The company folder contains an accented character; match around it rather
# than depending on the console's code page.
$roots = @()
$roots += Get-Item "$env:USERPROFILE\AppData\LocalLow\The Pok*mon Company International\Pokemon Trading Card Game Online"

# Also sweep every fixed drive, in case the profile was moved or restored.
foreach ($d in (Get-PSDrive -PSProvider FileSystem)) {
    $roots += Get-ChildItem -Path "$($d.Root)" -Filter "bundleCache" -Directory -Recurse -Depth 8 |
              ForEach-Object { $_.Parent }
}

$roots = $roots | Where-Object { $_ } | Sort-Object FullName -Unique
if (-not $roots) {
    Write-Host "No PTCGO cache found on this machine." -ForegroundColor Yellow
    return
}

foreach ($r in $roots) {
    $arch  = (Get-ChildItem "$($r.FullName)\archetypes" -File).Count
    $bund  = (Get-ChildItem "$($r.FullName)\bundleCache" -File).Count
    $mb    = [math]::Round((Get-ChildItem $r.FullName -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)

    Write-Host $r.FullName -ForegroundColor White
    Write-Host ("  archetypes (cards) : {0}" -f $arch)
    Write-Host ("  bundleCache (art)  : {0}" -f $bund)
    Write-Host ("  total size         : {0} MB" -f $mb)

    # A baseline-only install that never logged in sits around 9,900 cards and
    # a few dozen bundles. Anything well past that came from the live servers.
    if ($arch -gt 11000 -or $bund -gt 300) {
        Write-Host "  >> WORTH SENDING - this went well past the shipped baseline." -ForegroundColor Green
    } elseif ($arch -gt 0) {
        Write-Host "  >> Baseline only, probably not useful." -ForegroundColor Yellow
    }
    Write-Host ""
}

Write-Host "To send: zip the folder above, minus cake.cfg and output_log.txt" -ForegroundColor Cyan
Write-Host "(those two hold local config and logs, and are not needed)."
