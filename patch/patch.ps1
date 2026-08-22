param([string]$CecilDir, [string]$Managed, [string]$Helper)
Add-Type -Path (Join-Path $CecilDir 'Mono.Cecil.dll')

$target = Join-Path $Managed 'pie-bundles.dll'
$backup = "$target.orig"
if (-not (Test-Path $backup)) {
  Copy-Item $target $backup
  Write-Output "backed up -> $backup"
} else {
  Write-Output "restoring from backup before re-patching"
  Copy-Item $backup $target -Force
}

$res = New-Object Mono.Cecil.DefaultAssemblyResolver
$res.AddSearchDirectory($Managed)
$rp = New-Object Mono.Cecil.ReaderParameters
$rp.AssemblyResolver = $res
$rp.ReadWrite = $false
$rp.InMemory = $true

$helperAsm = [Mono.Cecil.AssemblyDefinition]::ReadAssembly($Helper, $rp)
$ensure = $helperAsm.MainModule.GetType('PtcgoLooseArt').Methods |
          Where-Object { $_.Name -eq 'Ensure' } | Select-Object -First 1
if (-not $ensure) { throw "Ensure() not found in helper" }

$asm = [Mono.Cecil.AssemblyDefinition]::ReadAssembly($target, $rp)
$mod = $asm.MainModule
$ensureRef = $mod.ImportReference($ensure)

$type = $mod.GetTypes() | Where-Object { $_.Name -eq 'AssetBundleImageCache' } | Select-Object -First 1
if (-not $type) { throw "AssetBundleImageCache not found" }
Write-Output "patching $($type.FullName)"

$patched = 0
foreach ($name in @('Contains','GetTexture')) {
  $m = $type.Methods | Where-Object {
        $_.Name -eq $name -and $_.HasBody -and $_.Parameters.Count -ge 1 -and
        $_.Parameters[0].ParameterType.FullName -eq 'System.String' }
  foreach ($method in $m) {
    $il = $method.Body.GetILProcessor()
    $first = $method.Body.Instructions[0]
    $il.InsertBefore($first, $il.Create([Mono.Cecil.Cil.OpCodes]::Ldarg_0))
    $il.InsertBefore($first, $il.Create([Mono.Cecil.Cil.OpCodes]::Ldarg_1))
    $il.InsertBefore($first, $il.Create([Mono.Cecil.Cil.OpCodes]::Call, $ensureRef))
    Write-Output ("  injected into {0}({1})" -f $method.Name,
      (($method.Parameters | ForEach-Object { $_.ParameterType.Name }) -join ', '))
    $patched++
  }
}
if ($patched -eq 0) { throw "nothing patched" }
$asm.Write($target)
Write-Output "wrote $target ($patched call sites)"
