@echo off
setlocal
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 (
    py -3 "%~dp0windows_install.py" install %*
    exit /b
)
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 (
    python "%~dp0windows_install.py" install %*
    exit /b
)
echo Error: Python 3.9 or newer is required. Install Python and enable its launcher or add it to PATH. >&2
exit /b 1
