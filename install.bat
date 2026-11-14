@echo off
rem Installs the `agh` command from a normal Command Prompt (double-click or run it).
rem   install.bat              install from this checkout
rem   install.bat -Editable    development: runs straight from the checkout
rem Any arguments are passed through to install.ps1, which does the work.
setlocal
where powershell >nul 2>nul
if errorlevel 1 (
    echo PowerShell was not found. It ships with Windows; install.ps1 needs it.
    goto :done
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 (
    echo.
    echo Install failed. See the message above.
) else (
    echo.
    echo Done. Open a new Command Prompt and run: agh
)
:done
endlocal
rem Keep the window open when launched by double-click.
echo %cmdcmdline% | find /i "%~nx0" >nul && pause
