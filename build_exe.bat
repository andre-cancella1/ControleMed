@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Ambiente Python nao encontrado. Crie .venv e instale requirements.txt.
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

set BUILD_WORK=%TEMP%\ControleMed-build-%RANDOM%
set BUILD_DIST=dist_auto_shortcut
".venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm --distpath "%BUILD_DIST%" --workpath "%BUILD_WORK%" ControleMed.spec
if errorlevel 1 exit /b 1

cscript //nologo "%~dp0create_shortcut.vbs" "%~dp0%BUILD_DIST%\ControleMed\ControleMed.exe" "%~dp0care_heh_hehy_medical_pills_remedy_medicine_icon_255235.ico"
if errorlevel 1 exit /b 1

echo.
echo Executavel gerado em: %BUILD_DIST%\ControleMed\ControleMed.exe
echo Atalho criado na area de trabalho: ControleMed.lnk