@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "ADDON=%cd%"
set "ZIP=%~dp0..\blender_lidartool.zip"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$addon = (Resolve-Path -LiteralPath $env:ADDON).Path;" ^
  "$zip = [IO.Path]::GetFullPath($env:ZIP);" ^
  "$skipDirs = @('.git','docs','node_modules','__pycache__','.pytest_cache');" ^
  "$skipFiles = @('README.md','LICENSE.md','release.bat','.gitignore','package.json','package-lock.json','vendor.js','Thumbs.db','.DS_Store');" ^
  "if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force };" ^
  "Add-Type -AssemblyName System.IO.Compression.FileSystem;" ^
  "$files = New-Object System.Collections.Generic.List[string];" ^
  "Get-ChildItem -LiteralPath $addon -Recurse -File -Force | ForEach-Object {" ^
  "  $rel = $_.FullName.Substring($addon.Length).TrimStart('\','/');" ^
  "  $parts = @($rel -split '[\\/]');" ^
  "  $drop = $false;" ^
  "  if ($parts.Length -gt 1) { foreach ($dir in $parts[0..($parts.Length-2)]) { if ($skipDirs -contains $dir) { $drop = $true } } };" ^
  "  if ($skipFiles -contains $_.Name) { $drop = $true };" ^
  "  if ($_.Extension -match '^\.(pyc|pyo|zip)$') { $drop = $true };" ^
  "  if (-not $drop) { $files.Add($rel) }" ^
  "};" ^
  "$archive = [IO.Compression.ZipFile]::Open($zip, 'Create');" ^
  "try {" ^
  "  foreach ($rel in $files) {" ^
  "    $source = Join-Path $addon $rel;" ^
  "    $name = 'blender_lidartool/' + ($rel -replace '\\','/');" ^
  "    [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $source, $name)" ^
  "  }" ^
  "} finally { $archive.Dispose() };" ^
  "Write-Output ('Vytvoreno: ' + $zip);" ^
  "Write-Output ('Souboru: ' + $files.Count)"

if errorlevel 1 exit /b 1
exit /b 0
