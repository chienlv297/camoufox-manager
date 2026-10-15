param(
    [switch]$Clean,
    [switch]$BundleCamoufox
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$Dist = Join-Path $Root "dist\CamoufoxProfileManager"
$Build = Join-Path $Root "build"
$Release = Join-Path $Root "release"
$BundleDir = Join-Path $Build "camoufox-bundle"

function Require-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name not found. $Hint"
    }
}

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE."
    }
}

function Remove-BuildDirectory([string]$Target) {
    if (-not (Test-Path -LiteralPath $Target)) { return }
    $Resolved = (Resolve-Path -LiteralPath $Target).Path
    $Prefix = $Root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $Resolved.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean path outside repository: $Resolved"
    }
    if ((Get-Item -LiteralPath $Resolved).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing to recursively clean a linked directory: $Resolved"
    }
    Remove-Item -LiteralPath $Resolved -Recurse -Force
}

Require-Command "uv" "Install uv for developer builds."
Require-Command "python" "Install Python 3.11 or newer for developer builds."
Require-Command "node" "Install Node.js 20.9+ for the static web UI build."
Require-Command "npm" "Install npm with Node.js."

$IsccCandidates = @(@(
    ((Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique)
if ($IsccCandidates.Count -eq 0) {
    throw "ISCC.exe not found. Install Inno Setup 6 before building the installer."
}
$Iscc = $IsccCandidates[0]
$OriginalVersionFile = $env:CPM_VERSION_FILE
$OriginalBundleDir = $env:CPM_BUNDLED_CAMOUFOX_DIR
$OriginalCacheDir = $env:CPM_CAMOUFOX_CACHE_DIR
$Pfx = $null
Push-Location -LiteralPath $Root
try {
    if ($Clean) {
        foreach ($Target in @($Build, (Join-Path $Root "dist"), $Release)) {
            Remove-BuildDirectory $Target
        }
    }
    New-Item -ItemType Directory -Force -Path $Build, $Release | Out-Null
    Invoke-Checked "uv" @("sync", "--extra", "build")
    Invoke-Checked "uv" @("run", "python", "-c", "import sys; sys.version_info >= (3, 11) or sys.exit('Windows builds require Python 3.11 or newer.')")
    $Version = (Invoke-Checked "uv" @("run", "python", "-c", "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")).Trim()
    $ParsedVersion = [regex]::Match($Version, '^(\d+)\.(\d+)\.(\d+)')
    if (-not $ParsedVersion.Success) { throw "Unsupported project version: $Version" }
    $VersionTuple = ($ParsedVersion.Groups[1..3].Value -join ',') + ',0'
    $VersionFile = Join-Path $Build "windows-version.txt"
    $VersionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($VersionTuple), prodvers=($VersionTuple), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [StringStruct('CompanyName', 'Camoufox Profile Manager Contributors'), StringStruct('FileDescription', 'Camoufox Profile Manager'), StringStruct('FileVersion', '$Version'), StringStruct('InternalName', 'CamoufoxProfileManager'), StringStruct('OriginalFilename', 'CamoufoxProfileManager.exe'), StringStruct('ProductName', 'Camoufox Profile Manager'), StringStruct('ProductVersion', '$Version')])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@
    [IO.File]::WriteAllText($VersionFile, $VersionInfo, [Text.UTF8Encoding]::new($false))
    Invoke-Checked "uv" @("run", "python", "scripts/build_webui.py")

    $env:CPM_BUNDLED_CAMOUFOX_DIR = $null
    $env:CPM_CAMOUFOX_CACHE_DIR = $null
    if ($BundleCamoufox) {
        Invoke-Checked "uv" @("run", "camoufox", "fetch")
        $InstalledCamoufox = (Invoke-Checked "uv" @("run", "python", "-c", "from camoufox.pkgman import INSTALL_DIR, camoufox_path, launch_path; launch_path(camoufox_path(download_if_missing=False)); print(INSTALL_DIR)")).Trim()
        if (-not (Test-Path -LiteralPath $InstalledCamoufox)) {
            throw "Camoufox fetch completed without an install directory: $InstalledCamoufox"
        }
        Remove-BuildDirectory $BundleDir
        Copy-Item -LiteralPath $InstalledCamoufox -Destination $BundleDir -Recurse -Force
        $env:CPM_BUNDLED_CAMOUFOX_DIR = $BundleDir
    }
    $env:CPM_VERSION_FILE = $VersionFile
    Invoke-Checked "uv" @("run", "pyinstaller", "packaging/camoufox-pm.spec", "--noconfirm", "--clean")

    $Exe = Join-Path $Dist "CamoufoxProfileManager.exe"
    foreach ($Required in @(
        $Exe,
        (Join-Path $Dist "_internal"),
        (Join-Path $Dist "_internal\camoufox_pm\webui\index.html"),
        (Join-Path $Dist "_internal\camoufox_pm\webui\_next"),
        (Join-Path $Dist "_internal\assets\app.ico"),
        (Join-Path $Dist "_internal\tzdata\zoneinfo\Europe\Paris"),
        (Join-Path $Dist "_internal\webview"),
        (Join-Path $Dist "_internal\playwright\driver\node.exe")
    )) {
        if (-not (Test-Path -LiteralPath $Required)) { throw "Build output missing: $Required" }
    }

    $SignTool = $null
    if ($env:WINDOWS_CERT_PFX) {
        $SignTool = (Get-Command signtool.exe -ErrorAction SilentlyContinue).Source
        if (-not $SignTool) {
            $SignTool = (Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue | Sort-Object FullName | Select-Object -Last 1).FullName
        }
        if (-not $SignTool) { throw "WINDOWS_CERT_PFX is set, but signtool.exe was not found." }
        $Pfx = [IO.Path]::GetTempFileName()
        [IO.File]::WriteAllBytes($Pfx, [Convert]::FromBase64String($env:WINDOWS_CERT_PFX))
        Invoke-Checked $SignTool @("sign", "/f", $Pfx, "/p", $env:WINDOWS_CERT_PASSWORD, "/fd", "SHA256", "/tr", "http://timestamp.digicert.com", "/td", "SHA256", $Exe)
    }

    Invoke-Checked $Iscc @("/DAppVersion=$Version", "/DSourceDir=$Dist", "/DOutputDir=$Release", (Join-Path $Root "packaging\windows\installer.iss"))
    $VersionedInstaller = Join-Path $Release "CamoufoxProfileManager-Setup-$Version.exe"
    $StableInstaller = Join-Path $Release "CamoufoxProfileManager-Setup.exe"
    if (-not (Test-Path -LiteralPath $VersionedInstaller)) { throw "Installer output missing: $VersionedInstaller" }
    if ($SignTool) {
        Invoke-Checked $SignTool @("sign", "/f", $Pfx, "/p", $env:WINDOWS_CERT_PASSWORD, "/fd", "SHA256", "/tr", "http://timestamp.digicert.com", "/td", "SHA256", $VersionedInstaller)
    }
    Copy-Item -LiteralPath $VersionedInstaller -Destination $StableInstaller -Force
    Write-Output "Installer: $StableInstaller"
    Write-Output "Versioned installer: $VersionedInstaller"
}
finally {
    if ($Pfx) { Remove-Item -LiteralPath $Pfx -Force -ErrorAction SilentlyContinue }
    $env:CPM_VERSION_FILE = $OriginalVersionFile
    $env:CPM_BUNDLED_CAMOUFOX_DIR = $OriginalBundleDir
    $env:CPM_CAMOUFOX_CACHE_DIR = $OriginalCacheDir
    Pop-Location
}
