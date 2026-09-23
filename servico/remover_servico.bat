@echo off
REM Remove o servico do Expansion 3D. Rode como administrador.
nssm.exe stop Expansion3D
nssm.exe remove Expansion3D confirm
