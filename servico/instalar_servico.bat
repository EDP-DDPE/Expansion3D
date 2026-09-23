@echo off
REM Instala o Expansion 3D como servico do Windows usando o NSSM.
REM Rode este arquivo como administrador. Ajuste NSSM e PYTHON se precisar.

setlocal
set RAIZ=%~dp0..
set NSSM=nssm.exe
set SERVICO=Expansion3D

for /f "delims=" %%P in ('where python') do set PYTHON=%%P& goto :achou
:achou

echo Raiz do projeto: %RAIZ%
echo Python: %PYTHON%

%NSSM% install %SERVICO% "%PYTHON%" "%RAIZ%\run_server.py"
%NSSM% set %SERVICO% AppDirectory "%RAIZ%"
%NSSM% set %SERVICO% DisplayName "Expansion 3D (Interplan)"
%NSSM% set %SERVICO% Description "Visualizador 3D de redes de distribuicao a partir de bases do Interplan"
%NSSM% set %SERVICO% Start SERVICE_AUTO_START
%NSSM% set %SERVICO% AppStdout "%RAIZ%\data\servico.log"
%NSSM% set %SERVICO% AppStderr "%RAIZ%\data\servico.log"
%NSSM% set %SERVICO% AppRotateFiles 1
%NSSM% set %SERVICO% AppRotateBytes 10485760
%NSSM% start %SERVICO%

echo.
echo Servico instalado. Acesse pela porta definida em conf.ini (padrao 8010).
echo Para liberar no firewall:
echo   netsh advfirewall firewall add rule name="Expansion 3D" dir=in action=allow protocol=TCP localport=8010
endlocal
