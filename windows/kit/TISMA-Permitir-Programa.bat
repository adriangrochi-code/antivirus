@echo off
rem TISMA - Permitir siempre un programa concreto que esta en una carpeta bloqueada.
rem Uso: arrastra el .exe sobre este archivo (como administrador) o: TISMA-Permitir-Programa.bat "C:\ruta\programa.exe"
setlocal EnableExtensions DisableDelayedExpansion
net session >nul 2>&1
if errorlevel 1 (
    echo Hace falta permiso de administrador: clic derecho, "Ejecutar como administrador".
    pause
    exit /b 1
)
if "%~1"=="" (
    echo Arrastra el programa sobre este archivo, o indica su ruta.
    pause
    exit /b 2
)
if not exist "%~f1" (
    echo No existe: %~f1
    pause
    exit /b 2
)
echo Se permitira SIEMPRE: %~f1
echo Hazlo solo con programas que conoces y descargaste de su web oficial.
set "OK="
set /p "OK=Escribe S y pulsa Intro para seguir: "
if /i not "%OK%"=="S" exit /b 0
set "H=0123456789ABCDEF"
set "G="
setlocal EnableDelayedExpansion
for /l %%i in (1,1,12) do (
    set /a "x=!random! %% 16"
    for %%x in (!x!) do set "G=!G!!H:~%%x,1!"
)
endlocal & set "G=%G%"
set "K=HKLM\SOFTWARE\Policies\Microsoft\Windows\Safer\CodeIdentifiers\262144\Paths\{7154A5E0-7153-4D41-9000-%G%}"
reg add "%K%" /v ItemData /t REG_EXPAND_SZ /d "%~f1" /f >nul
reg add "%K%" /v SaferFlags /t REG_DWORD /d 0 /f >nul
reg add "%K%" /v Description /t REG_SZ /d "TISMA: permitido por el usuario" /f >nul
if errorlevel 1 (
    echo [!] No se pudo.
    pause
    exit /b 1
)
echo Permitido. Para quitarlo: regedit, %K%
pause
