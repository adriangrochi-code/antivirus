@echo off
rem TISMA - Endurecer Windows XP, Vista, 7, 8 y 8.1 (kit de Escudo)
rem Cierra lo que usan los ataques de red y el malware que llega por descargas, USB o correo.
rem Cada cambio se apunta en TISMA-Deshacer.bat (en la carpeta de la copia) para poder volver atras.
setlocal EnableExtensions DisableDelayedExpansion
title TISMA - Endurecer Windows

net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo  Hace falta permiso de administrador:
    echo  clic derecho en TISMA-Endurecer.bat y "Ejecutar como administrador".
    echo.
    pause
    exit /b 1
)

rem ---------------------------------------------------------------- version de Windows
set "VER="
for /f "tokens=3" %%v in ('reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion" /v CurrentVersion 2^>nul ^| findstr /i "REG_SZ"') do set "VER=%%v"
set "MAYOR="
for /f "tokens=3" %%v in ('reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion" /v CurrentMajorVersionNumber 2^>nul ^| findstr /i "REG_DWORD"') do set "MAYOR=%%v"
set "NOMBRE="
if "%VER%"=="5.1" set "NOMBRE=XP"
if "%VER%"=="5.2" set "NOMBRE=XP x64 / Server 2003"
if "%VER%"=="6.0" set "NOMBRE=Vista"
if "%VER%"=="6.1" set "NOMBRE=7"
if "%VER%"=="6.2" set "NOMBRE=8"
if "%VER%"=="6.3" set "NOMBRE=8.1"
if defined MAYOR set "NOMBRE="
if not defined NOMBRE (
    echo.
    echo  Este kit es para Windows XP, Vista, 7, 8 y 8.1. Este equipo tiene otra version: no se cambia nada.
    echo.
    pause
    exit /b 1
)
set "XP="
if "%VER:~0,2%"=="5." set "XP=1"
set "X64="
if exist "%SystemRoot%\SysWOW64\" set "X64=1"

echo.
echo  ============================================================
echo   TISMA - Endurecer Windows %NOMBRE%
echo  ============================================================
echo.
echo  Se va a:
echo   - cerrar la red a las conexiones de fuera (cortafuegos, SMBv1, NetBIOS,
echo     escritorio remoto, servicios que no hacen falta en casa)
echo   - impedir que se ejecuten programas desde Descargas, Temp y AppData
echo     (por donde entra casi todo el malware)
echo   - activar las protecciones de memoria (DEP, SEHOP) y apagar AutoRun y
echo     los scripts de Windows (.vbs, .js, .hta)
echo.
echo  Todo se puede deshacer: se guarda TISMA-Deshacer.bat con los valores de antes.
echo  Para instalar un programa descargado usa TISMA-Permitir-Instalar.bat.
echo.
set "OK="
set /p "OK=Escribe S y pulsa Intro para seguir: "
if /i not "%OK%"=="S" exit /b 0

rem ---------------------------------------------------------------- copia y deshacer
set "BASE=%SystemDrive%\TISMA-Kit"
set "YA="
reg query "HKLM\SOFTWARE\TISMA\Kit" /v Copia >nul 2>&1
if not errorlevel 1 set "YA=1"
if defined YA (
    for /f "tokens=2,*" %%a in ('reg query "HKLM\SOFTWARE\TISMA\Kit" /v Copia ^| findstr /i "REG_SZ"') do set "COPIA=%%b"
    set "DESHACER=nul"
    echo.
    echo  El kit ya se habia aplicado. Se vuelve a aplicar ^(por si algo se cambio^);
    echo  para deshacer sigue valiendo la copia original.
    goto :copia_lista
)
set /a N=1
:buscar_copia
if exist "%BASE%\copia-%N%\" (
    set /a N+=1
    goto :buscar_copia
)
set "COPIA=%BASE%\copia-%N%"
mkdir "%COPIA%" >nul 2>&1
if not exist "%COPIA%\" (
    echo No se pudo crear %COPIA%
    pause
    exit /b 1
)
set "DESHACER=%COPIA%\TISMA-Deshacer.bat"
copy /y "%~dp0plantilla-deshacer.txt" "%DESHACER%" >nul
if errorlevel 1 (
    echo Falta plantilla-deshacer.txt junto a este archivo: copia la carpeta del kit completa.
    pause
    exit /b 1
)
:copia_lista
set /a FALLOS=0
set /a CAMBIOS=0
set "LOG=%BASE%\endurecer.log"
if not exist "%BASE%\" mkdir "%BASE%"
>>"%LOG%" echo ==== %DATE% %TIME% Windows %NOMBRE% (%VER%)

echo.
echo  Creando un punto de restauracion...
wmic /namespace:\\root\default path SystemRestore call CreateRestorePoint "TISMA antes de endurecer", 100, 7 >nul 2>&1
if errorlevel 1 echo    ^(no se pudo: sigue igual, TISMA-Deshacer.bat sirve para volver atras^)

rem ================================================================ 1. RED
echo.
echo  [1/6] Red
set "S=HKLM\SYSTEM\CurrentControlSet\Services"
if defined XP goto :red_xp

rem --- Vista y posteriores: cortafuegos con copia completa (el deshacer la reimporta)
if not defined YA (
    netsh advfirewall export "%COPIA%\cortafuegos.wfw" >nul 2>&1
    if exist "%COPIA%\cortafuegos.wfw" >>"%DESHACER%" echo netsh advfirewall import "%%~dp0cortafuegos.wfw"
)
netsh advfirewall set allprofiles state on >nul
netsh advfirewall set allprofiles firewallpolicy blockinbound,allowoutbound >nul
call :regla_entrada "SMB y NetBIOS" TCP 135,139,445
call :regla_entrada "NetBIOS y descubrimiento" UDP 137,138,1900,5355
call :regla_entrada "escritorio remoto y administracion remota" TCP 2869,3389,5985,5986
rem Programas que el malware usa para descargar o ejecutar codigo: sin Internet
for %%p in (mshta wscript cscript regsvr32 certutil hh bitsadmin) do (
    for %%d in (System32 SysWOW64) do (
        if exist "%SystemRoot%\%%d\%%p.exe" (
            netsh advfirewall firewall delete rule name="TISMA: sin Internet para %%p (%%d)" >nul 2>&1
            netsh advfirewall firewall add rule name="TISMA: sin Internet para %%p (%%d)" dir=out action=block program="%SystemRoot%\%%d\%%p.exe" >nul
        )
    )
)
echo    cortafuegos: entradas bloqueadas, SMB/NetBIOS/RDP cerrados, sin Internet para mshta, wscript, regsvr32...
rem SMBv1 (WannaCry, EternalBlue): servidor y cliente
call :poner "%S%\LanmanServer\Parameters" SMB1 REG_DWORD 0
reg query "%S%\mrxsmb10" >nul 2>&1
if not errorlevel 1 (
    call :poner "%S%\mrxsmb10" Start REG_DWORD 4
    call :poner "%S%\LanmanWorkstation" DependOnService REG_MULTI_SZ "Bowser\0MRxSmb20\0NSI"
)
rem LLMNR: respuestas falsas en la red local para robar contrasenas (Responder)
call :poner "HKLM\SOFTWARE\Policies\Microsoft\Windows NT\DNSClient" EnableMulticast REG_DWORD 0
echo    SMBv1 y LLMNR apagados
goto :red_comun

:red_xp
rem --- XP: el cortafuegos se guarda en el registro (asi el deshacer es exacto)
for %%f in (StandardProfile DomainProfile) do (
    call :poner "%S%\SharedAccess\Parameters\FirewallPolicy\%%f" EnableFirewall REG_DWORD 1
    call :poner "%S%\SharedAccess\Parameters\FirewallPolicy\%%f" DoNotAllowExceptions REG_DWORD 1
)
netsh firewall set opmode mode=ENABLE exceptions=DISABLE profile=ALL >nul 2>&1
call :servicio SharedAccess 2
rem En XP el servicio Servidor es SMBv1 sin remedio (MS08-067, MS17-010): se apaga
call :servicio lanmanserver 4
echo    cortafuegos sin excepciones y servicio Servidor apagado ^(compartir carpetas deja de funcionar^)

:red_comun
rem NetBIOS sobre TCP/IP en todas las tarjetas
for /f "delims=" %%k in ('reg query "%S%\NetBT\Parameters\Interfaces" 2^>nul ^| findstr /i "Tcpip_"') do call :poner "%%k" NetbiosOptions REG_DWORD 2
rem Escritorio remoto y Asistencia remota (BlueKeep)
call :poner "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server" fDenyTSConnections REG_DWORD 1
call :poner "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server" fAllowToGetHelp REG_DWORD 0
call :poner "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" UserAuthentication REG_DWORD 1
reg query "HKLM\SYSTEM\CurrentControlSet\Control\Remote Assistance" >nul 2>&1
if not errorlevel 1 call :poner "HKLM\SYSTEM\CurrentControlSet\Control\Remote Assistance" fAllowToGetHelp REG_DWORD 0
rem Servicios de red que en casa no hacen falta y han tenido fallos graves
for %%s in (RemoteRegistry SSDPSRV upnphost WebClient TlntSvr SNMP Messenger NetDDE NetDDEdsdm RDSessMgr WMPNetworkSvc RemoteAccess Browser lmhosts HomeGroupListener HomeGroupProvider WinHttpAutoProxySvc WinRM FDResPub Mcx2Svc) do call :servicio %%s 4
echo    NetBIOS, escritorio remoto, asistencia remota y servicios de red innecesarios apagados

rem ================================================================ 2. CREDENCIALES
echo.
echo  [2/6] Contrasenas y acceso anonimo
set "L=HKLM\SYSTEM\CurrentControlSet\Control\Lsa"
call :poner "%L%" LmCompatibilityLevel REG_DWORD 5
call :poner "%L%" NoLMHash REG_DWORD 1
call :poner "%L%" RestrictAnonymous REG_DWORD 1
call :poner "%L%" RestrictAnonymousSAM REG_DWORD 1
call :poner "%L%" EveryoneIncludesAnonymous REG_DWORD 0
reg query "HKLM\SYSTEM\CurrentControlSet\Control\SecurityProviders\WDigest" >nul 2>&1
if not errorlevel 1 call :poner "HKLM\SYSTEM\CurrentControlSet\Control\SecurityProviders\WDigest" UseLogonCredential REG_DWORD 0
set "P=HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
if not defined XP (
    call :poner "%P%" EnableLUA REG_DWORD 1
    call :poner "%P%" ConsentPromptBehaviorAdmin REG_DWORD 2
    call :poner "%P%" PromptOnSecureDesktop REG_DWORD 1
)
echo    solo NTLMv2, sin hashes LM, sin acceso anonimo; UAC preguntando siempre en el escritorio seguro

rem ================================================================ 3. MEMORIA
echo.
echo  [3/6] Protecciones de memoria
if defined XP goto :dep_xp
set "NX="
for /f "tokens=1,2" %%a in ('bcdedit /enum {current} 2^>nul') do if /i "%%a"=="nx" set "NX=%%b"
if not defined YA (
    if defined NX (>>"%DESHACER%" echo bcdedit /set {current} nx %NX%) else (>>"%DESHACER%" echo bcdedit /deletevalue {current} nx)
)
bcdedit /set {current} nx AlwaysOn >nul 2>&1
if errorlevel 1 (
    echo    [!] no se pudo activar DEP para todos
    set /a FALLOS+=1
) else (
    set /a CAMBIOS+=1
)
call :poner "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\kernel" DisableExceptionChainValidation REG_DWORD 0
echo    DEP para todos los programas y SEHOP activados
goto :dep_fin
:dep_xp
set "BI=%SystemDrive%\boot.ini"
findstr /i /c:"noexecute=alwayson" "%BI%" >nul 2>&1
if not errorlevel 1 goto :dep_xp_ya
if not defined YA (
    copy /y "%BI%" "%COPIA%\boot.ini" >nul 2>&1
    >>"%DESHACER%" echo attrib -s -h -r "%%SystemDrive%%\boot.ini" ^& copy /y "%%~dp0boot.ini" "%%SystemDrive%%\boot.ini" ^& attrib +s +h +r "%%SystemDrive%%\boot.ini"
)
bootcfg /raw "/noexecute=alwayson /fastdetect" /id 1 >nul 2>&1
if errorlevel 1 (
    echo    [!] no se pudo cambiar boot.ini
    set /a FALLOS+=1
) else (
    set /a CAMBIOS+=1
)
:dep_xp_ya
echo    DEP para todos los programas activado
:dep_fin

rem ================================================================ 4. AUTORUN Y SCRIPTS
echo.
echo  [4/6] USB, CD y scripts
set "E=HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer"
call :poner "%E%" NoDriveTypeAutoRun REG_DWORD 255
call :poner "%E%" NoAutorun REG_DWORD 1
reg query "%S%\Cdrom" /v Start >nul 2>&1
if not errorlevel 1 call :poner "%S%\Cdrom" AutoRun REG_DWORD 0
set "IFM=HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\IniFileMapping\Autorun.inf"
reg query "%IFM%" >nul 2>&1
if errorlevel 1 (
    >>"%DESHACER%" echo reg delete "%IFM%" /f ^>nul 2^>^&1
    reg add "%IFM%" /ve /t REG_SZ /d "@SYS:DoesNotExist" /f >nul
    set /a CAMBIOS+=1
)
rem Windows Script Host (.vbs, .js, .wsf): el adjunto de correo malicioso de siempre
call :poner "HKLM\SOFTWARE\Microsoft\Windows Script Host\Settings" Enabled REG_DWORD 0
if defined X64 call :poner "HKLM\SOFTWARE\Wow6432Node\Microsoft\Windows Script Host\Settings" Enabled REG_DWORD 0
rem .hta se abre con el Bloc de notas en vez de ejecutarse
set "HTA=HKLM\SOFTWARE\Classes\htafile\shell\open\command"
reg query "%HTA%" >nul 2>&1
if not errorlevel 1 (
    if not defined YA (
        reg export "%HTA%" "%COPIA%\hta.reg" >nul 2>&1
        >>"%DESHACER%" echo reg import "%%~dp0hta.reg"
    )
    reg add "%HTA%" /ve /t REG_EXPAND_SZ /d "\"%%SystemRoot%%\system32\notepad.exe\" \"%%1\"" /f >nul
    set /a CAMBIOS+=1
)
echo    AutoRun apagado en todas las unidades; .vbs, .js y .hta ya no se ejecutan

rem ================================================================ 5. IMPRESION
echo.
echo  [5/6] Impresoras
set "PR=HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Printers"
call :poner "%PR%" RegisterSpoolerRemoteRpcEndPoint REG_DWORD 2
call :poner "%PR%\PointAndPrint" RestrictDriverInstallationToAdministrators REG_DWORD 1
echo    la cola de impresion no acepta conexiones de la red ^(PrintNightmare^)

rem ================================================================ 6. EJECUCION
echo.
echo  [6/6] Programas desde Descargas, Temp y AppData
call :srp
echo    bloqueados ^(para instalar algo descargado: TISMA-Permitir-Instalar.bat^)

rem ---------------------------------------------------------------- fin
reg add "HKLM\SOFTWARE\TISMA\Kit" /v Copia /t REG_SZ /d "%COPIA%" /f >nul
if not defined YA (
    >>"%DESHACER%" echo reg delete "HKLM\SOFTWARE\TISMA\Kit" /f ^>nul 2^>^&1
    >>"%DESHACER%" echo echo.
    >>"%DESHACER%" echo echo  Listo. Reinicia el equipo para que todo vuelva a como estaba.
    >>"%DESHACER%" echo pause
    copy /y "%~dp0TISMA-Permitir-Instalar.bat" "%BASE%\" >nul 2>&1
    copy /y "%~dp0TISMA-Permitir-Programa.bat" "%BASE%\" >nul 2>&1
    copy /y "%~dp0LEEME.txt" "%BASE%\" >nul 2>&1
)
>>"%LOG%" echo cambios=%CAMBIOS% fallos=%FALLOS%
echo.
echo  ============================================================
echo   Hecho: %CAMBIOS% cambios, %FALLOS% avisos.
echo   Para deshacer: %COPIA%\TISMA-Deshacer.bat
echo   ^(como administrador^). REINICIA el equipo para que todo se aplique.
echo  ============================================================
echo.
pause
exit /b 0


rem ================================================================ subrutinas
:poner
rem %1 clave  %2 valor  %3 tipo  %4 dato. Apunta el valor anterior en el deshacer y lo cambia.
call :anotar %1 %2
reg add "%~1" /v "%~2" /t %~3 /d %4 /f >nul 2>&1
if errorlevel 1 (
    echo    [!] no se pudo cambiar %~2 en %~1
    >>"%LOG%" echo FALLO %~1 %~2
    set /a FALLOS+=1
) else (
    set /a CAMBIOS+=1
)
goto :eof

:anotar
rem %1 clave  %2 valor: escribe en el deshacer como devolverlo a lo que vale ahora (o borrarlo si no existe)
set "P_ANT="
for /f "tokens=1,2,*" %%a in ('reg query "%~1" /v "%~2" 2^>nul ^| findstr /i /c:"REG_"') do (
    if /i "%%a"=="%~2" (
        set "P_ANT=1"
        >>"%DESHACER%" echo reg add "%~1" /v "%~2" /t %%b /d "%%c" /f ^>nul
    )
)
if not defined P_ANT >>"%DESHACER%" echo reg delete "%~1" /v "%~2" /f ^>nul 2^>^&1
goto :eof

:servicio
rem %1 servicio  %2 inicio (2 automatico, 4 desactivado). Solo si existe.
reg query "%S%\%~1" /v Start >nul 2>&1
if errorlevel 1 goto :eof
call :poner "%S%\%~1" Start REG_DWORD %~2
if "%~2"=="4" sc stop "%~1" >nul 2>&1
goto :eof

:regla_entrada
rem %1 descripcion  %2 protocolo  %3 puertos
netsh advfirewall firewall delete rule name="TISMA: bloquear %~1 (%~2)" >nul 2>&1
netsh advfirewall firewall add rule name="TISMA: bloquear %~1 (%~2)" dir=in action=block protocol=%~2 localport=%~3 >nul
if errorlevel 1 (set /a FALLOS+=1) else (set /a CAMBIOS+=1)
goto :eof

:srp
rem Directivas de restriccion de software (todas las ediciones, desde XP): reglas de ruta "No permitido".
set "CI=HKLM\SOFTWARE\Policies\Microsoft\Windows\Safer\CodeIdentifiers"
rem Windows suele traer esta clave con los valores por defecto (sin reglas): eso no es un conflicto.
rem Si ya hay REGLAS que no son de TISMA (otro administrador o programa), no se toca nada.
reg query "%CI%" /v TISMA >nul 2>&1
if not errorlevel 1 goto :srp_aplicar
for %%n in (0 4096 65536 131072 262144) do (
    reg query "%CI%\%%n" >nul 2>&1
    if not errorlevel 1 (
        echo    [!] ya hay directivas de restriccion de software de otro origen: no se tocan
        set /a FALLOS+=1
        goto :eof
    )
)
reg query "%CI%" >nul 2>&1
if errorlevel 1 (
    >>"%DESHACER%" echo reg delete "%CI%" /f ^>nul 2^>^&1
) else (
    for %%v in (DefaultLevel TransparentEnabled PolicyScope AuthenticodeEnabled ExecutableTypes) do call :anotar "%CI%" %%v
    >>"%DESHACER%" echo reg delete "%CI%\0" /f ^>nul 2^>^&1
    >>"%DESHACER%" echo reg delete "%CI%\262144" /f ^>nul 2^>^&1
    >>"%DESHACER%" echo reg delete "%CI%" /v TISMA /f ^>nul 2^>^&1
)
:srp_aplicar
rem Las reglas van en tisma-srp.reg (generado con los %VARIABLE% literales: cmd los expandiria aqui)
reg import "%~dp0tisma-srp.reg" >nul 2>&1
if errorlevel 1 (
    echo    [!] no se pudieron poner las reglas ^(falta tisma-srp.reg junto a este archivo?^)
    set /a FALLOS+=1
) else (
    set /a CAMBIOS+=1
)
goto :eof
