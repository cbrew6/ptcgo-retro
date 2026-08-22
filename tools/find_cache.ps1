# Scans every drive for a PTCGO bundleCache and reports whether it holds
# real card art (worth sending) or only the cosmetics every install has.
#   powershell -ExecutionPolicy Bypass -File find_cache.ps1
$found = $false
foreach ($d in Get-PSDrive -PSProvider FileSystem) {
  $glob = Join-Path $d.Root 'Users\*\AppData\LocalLow\*Pok*mon Company International\*Trading Card Game Online\bundleCache'
  foreach ($c in (Get-Item $glob -ErrorAction SilentlyContinue)) {
    $found = $true
    $dirs = @(Get-ChildItem $c.FullName -Directory -ErrorAction SilentlyContinue)
    $sum  = (Get-ChildItem $c.FullName -Recurse -File -ErrorAction SilentlyContinue |
             Measure-Object Length -Sum).Sum
    Write-Output ""
    Write-Output $c.FullName
    Write-Output ("  {0} folders, {1:N2} GB" -f $dirs.Count, ($sum/1GB))
    # XY12 and the energy sets ship with every install - they are NOT evidence
    # of a real cache. Backgrounds and any other set code are.
    $hits = $dirs | Where-Object {
      $_.Name -match 'Background' -or
      ($_.Name -match '_(BW|XY|SM|HGSS|COL|DV|SL|RSP|TK)\d' -and
       $_.Name -notmatch 'XY12|Energy')
    }
    if ($hits) {
      Write-Output ("  *** SET/BACKGROUND ART FOUND: " +
        (($hits | Select-Object -First 8 -ExpandProperty Name) -join ', '))
    } else {
      Write-Output "  (cosmetics only - same as a fresh install, not worth sending)"
    }
  }
}
if (-not $found) { Write-Output "No PTCGO bundleCache found on any drive." }
