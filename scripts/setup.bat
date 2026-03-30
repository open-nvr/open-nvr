@echo off
echo Setting up OpenNVR Surveillance System API Virtual Environment...
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python is not installed or not in PATH
    echo Please install Python from https://python.org
    echo.
    echo Note: Python 3.11 or 3.12 is recommended to avoid compatibility issues
    pause
    exit /b 1
)

echo Python found, checking version...
python --version

echo.
echo Note: If you encounter Rust compilation errors, the setup script will:
echo   - Automatically detect Python 3.13 compatibility issues
echo   - Offer to download and install Python 3.11 automatically
echo   - Guide you through the installation process
echo.

echo Creating virtual environment...
python setup_venv.py

if errorlevel 1 (
    echo.
    echo Setup failed. Please check the error messages above.
    echo.
    echo If you're using Python 3.13 and getting Rust errors, the script will:
    echo   1. Offer automatic Python 3.11 installation
    echo   2. Download and install Python 3.11 for you
    echo   3. Guide you through the process
    echo.
    echo Alternative solutions:
    echo   1. Install Python 3.11 manually from https://python.org
    echo   2. Use Docker: docker-compose up --build
    pause
    exit /b 1
)

echo.
echo Setup completed successfully!
echo.
echo To activate the virtual environment, run:
echo   activate_venv.bat
echo.
echo Or manually activate with:
echo   venv\Scripts\activate
echo.
pause
