@echo off
rem TISMA - Pausa el bloqueo de programas (Descargas, Temp, AppData) mientras instalas algo.
rem Al pulsar una tecla, o si cierras esta ventana y vuelves a abrirla, el bloqueo se restaura.
setlocal EnableExtensions DisableDelayedExpansion
title TISMA - Permitir instalar
net session >nul 2>&1
if errorlevel 1 (
    echo Hace falta permiso de administrador: clic derecho, "Ejecutar como administrador".
    pause
    exit /b 1
)
set "CI=HKLM\SOFTWARE\Policies\Microsoft\Windows\Safer\CodeIdentifiers"
set "GUARDA=%SystemDrive%\TISMA-Kit\bloqueo-pausado.reg"
if exist "%GUARDA%" (
    echo Habia una pausa sin terminar: se restaura el bloqueo primero.
    reg import "%GUARDA%" >nul 2>&1
    if not errorlevel 1 del "%GUARDA%"
)
reg query "%CI%\0\Paths" >nul 2>&1
if errorlevel 1 (
    echo El bloqueo de TISMA no esta activo.
    pause
    exit /b 0
)
reg export "%CI%\0\Paths" "%GUARDA%" >nul
if not exist "%GUARDA%" (
    echo No se pudo guardar el bloqueo: no se pausa.
    pause
    exit /b 1
)
reg delete "%CI%\0\Paths" /f >nul
echo.
echo  ============================================================
echo   Bloqueo PAUSADO. Instala ahora el programa (solo de su web oficial).
echo   Cuando termines, vuelve aqui y pulsa una tecla para reactivarlo.
echo  ============================================================
echo.
pause
reg import "%GUARDA%" >nul 2>&1
if errorlevel 1 (
    echo [!] No se pudo restaurar: vuelve a ejecutar TISMA-Endurecer.bat
    pause
    exit /b 1
)
del "%GUARDA%"
echo Bloqueo reactivado.
pause
