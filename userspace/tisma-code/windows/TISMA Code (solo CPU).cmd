@echo off
rem Igual que "TISMA Code.cmd", pero con llama.cpp solo para CPU (si la grafica o su controlador Vulkan fallan).
setlocal
set "TISMA_LLAMA=cpu"
call "%~dp0TISMA Code.cmd" %*
