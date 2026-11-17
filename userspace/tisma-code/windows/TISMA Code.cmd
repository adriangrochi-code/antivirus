@echo off
rem TISMA Code (portable): Python embebido + llama.cpp de esta carpeta. Muestra la consola con el registro;
rem para abrirlo sin consola, usa "TISMA Code.vbs".
setlocal
set "RAIZ=%~dp0"
if not defined TISMA_LLAMA set "TISMA_LLAMA=vulkan"
set "PATH=%RAIZ%llama\%TISMA_LLAMA%;%PATH%"
cd /d "%USERPROFILE%"
rem -I: sin variables PYTHON* ni la carpeta actual en sys.path (solo lo que fija python\python312._pth)
"%RAIZ%python\python.exe" -X utf8 -I -m tisma_code %*
