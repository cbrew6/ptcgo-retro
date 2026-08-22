# Decrypt the client's obfuscated string table.
#
# String literals live in a <PrivateImplementationDetails> type as parameterless
# static methods that decrypt on first call. ILSpy renders their names with
# substitutions, so they cannot be matched by the names in the decompiled
# source - but every one can simply be invoked and the results dumped.

$ErrorActionPreference = 'Stop'
$managed = $args[0]
$out = $args[1]

$asm = [System.Reflection.Assembly]::LoadFrom((Join-Path $managed 'pie-src.dll'))
# Several types match that name; the one holding the strings is the GUID-suffixed
# one. Pick by which actually has string-returning methods rather than by name.
$t = $asm.GetTypes() |
    Where-Object { $_.FullName -like '*PrivateImplementationDetails*' } |
    Sort-Object { $_.GetMethods([System.Reflection.BindingFlags]'Static,Public,NonPublic,DeclaredOnly').Count } -Descending |
    Select-Object -First 1
if (-not $t) { throw 'no PrivateImplementationDetails type' }

$flags = [System.Reflection.BindingFlags]'Static,Public,NonPublic'
$methods = $t.GetMethods($flags) | Where-Object {
    $_.ReturnType -eq [string] -and $_.GetParameters().Count -eq 0
}
Write-Output ("candidate methods: " + $methods.Count)

$sb = New-Object System.Text.StringBuilder
$ok = 0
foreach ($m in $methods) {
    try {
        $v = $m.Invoke($null, @())
        if ($null -ne $v) {
            [void]$sb.AppendLine(($m.Name -replace "`t", ' ') + "`t" + ($v -replace "`r?`n", ' '))
            $ok++
        }
    } catch { }
}
[System.IO.File]::WriteAllText($out, $sb.ToString(), (New-Object System.Text.UTF8Encoding($false)))
Write-Output ("decrypted: " + $ok)
