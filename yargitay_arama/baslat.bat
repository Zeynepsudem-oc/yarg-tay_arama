@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Yargitay 11. Hukuk Dairesi Karar Arama baslatiliyor...
echo (Bu pencereyi kapatirsaniz program durur.)
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    py app.py
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        python app.py
    ) else (
        echo HATA: Python bulunamadi. Once Python'i kurmaniz gerekiyor.
        echo https://www.python.org/downloads/ adresinden indirip kurabilirsiniz.
        echo Kurulum sirasinda "Add python.exe to PATH" kutusunu isaretlemeyi unutmayin.
        pause
        exit /b 1
    )
)

pause