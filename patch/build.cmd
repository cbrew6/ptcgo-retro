@echo off
REM Rebuild and install the loose-art helper.
REM
REM This replaces <Managed>\PtcgoLooseArt.dll only. No IL re-injection is
REM needed: pie-bundles.dll already calls PtcgoLooseArt.Ensure(object, string)
REM at the top of AssetBundleImageCache.Contains and .GetTexture, and the
REM assembly identity (PtcgoLooseArt, 0.0.0.0) and that signature are
REM unchanged. Close the game first - the DLL cannot be overwritten while the
REM process holds it.
REM
REM To revert: copy PtcgoLooseArt.dll.bak_untracked back over the installed
REM DLL. To rebuild the IL injection itself, see patch\patch.ps1.

setlocal
set MANAGED=%APPDATA%\Pokémon Trading Card Game Online\PokemonTradingCardGameOnline\Pokemon Trading Card Game Online_Data\Managed
set CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe

"%CSC%" -nologo -target:library -out:"%~dp0PtcgoLooseArt.dll" ^
    -r:"%MANAGED%\UnityEngine.dll" ^
    -r:"%MANAGED%\UnityEngine.CoreModule.dll" ^
    -r:"%MANAGED%\UnityEngine.ImageConversionModule.dll" ^
    "%~dp0PtcgoLooseArt.cs"
if errorlevel 1 exit /b 1

copy /y "%~dp0PtcgoLooseArt.dll" "%MANAGED%\PtcgoLooseArt.dll"
