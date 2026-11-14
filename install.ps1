# Installs the `agh` command for the current user on Windows.
#   powershell -ExecutionPolicy Bypass -File install.ps1            # from a checkout of this repo
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Source <path-or-git-url>
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Editable  # development: runs straight from the checkout
# Uses uv if present, then pipx, then a private venv in %USERPROFILE%\.agh\venv.
param([string]$Source = $PSScriptRoot, [switch]$Editable)
$ErrorActionPreference = "Stop"

function Has($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

# Native commands don't stop the script on failure, so check each one.
function Run([string]$what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE). AGH was not installed." }
}

# A local checkout is built from a clean copy in %TEMP%: building in place writes a build\ folder into the
# checkout, which can hold stale files or be locked by an editor or indexer (WinError 32).
$target = $Source
$stage = $null
if (-not $Editable -and (Test-Path (Join-Path $Source "pyproject.toml"))) {
    $stage = Join-Path ([IO.Path]::GetTempPath()) ("agh-install-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force $stage | Out-Null
    foreach ($item in @("pyproject.toml", "README.md", "src")) {
        $from = Join-Path $Source $item
        if (Test-Path $from) { Copy-Item -Recurse -Force $from $stage }
    }
    Get-ChildItem -Recurse -Force -Directory $stage -Filter "__pycache__" | Remove-Item -Recurse -Force
    Get-ChildItem -Force -Directory (Join-Path $stage "src") -Filter "*.egg-info" | Remove-Item -Recurse -Force
    $target = $stage
}

try {
    if (Has "uv") {
        Write-Host "Installing with uv..."
        if ($Editable) { uv tool install --force --editable $target }
        else { uv tool install --force --reinstall --refresh $target }  # never reuse a cached older build
        Run "uv tool install"
        uv tool update-shell | Out-Null
    } elseif (Has "pipx") {
        Write-Host "Installing with pipx..."
        if ($Editable) { pipx install --force --editable $target }
        else { pipx install --force --pip-args="--no-cache-dir" $target }
        Run "pipx install"
        pipx ensurepath | Out-Null
    } else {
        $python = if (Has "py") { "py" } elseif (Has "python") { "python" } else { throw "Python 3.11+ is required: https://www.python.org/downloads/" }
        $home_ = if ($env:AGH_HOME) { $env:AGH_HOME } else { Join-Path $env:USERPROFILE ".agh" }
        $venv = Join-Path $home_ "venv"
        $bin = Join-Path $home_ "bin"
        Write-Host "Installing into $venv..."
        & $python -m venv $venv; Run "creating the venv"
        $pip = Join-Path $venv "Scripts\python.exe"
        & $pip -m pip install --upgrade pip | Out-Null
        if ($Editable) { & $pip -m pip install -e $target }
        else { & $pip -m pip install --upgrade --force-reinstall --no-cache-dir $target }
        Run "pip install"
        New-Item -ItemType Directory -Force $bin | Out-Null
        Set-Content -Encoding ascii (Join-Path $bin "agh.cmd") "@`"$venv\Scripts\agh.exe`" %*"
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if (($userPath -split ";") -notcontains $bin) {
            [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User")
            Write-Host "Added $bin to your PATH."
        }
    }
} finally {
    if ($stage) { Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue }
}

Write-Host ""
Write-Host "Installed. Open a new terminal, then:"
Write-Host "  cd <any folder>; agh       # that folder becomes the workspace"
Write-Host "  The first start asks which provider to use and tests your key (run /setup later to change it)."
