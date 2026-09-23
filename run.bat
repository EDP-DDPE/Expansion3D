@echo off
rem Sobe o Expansion 3D em primeiro plano (para testes). Em servidor, use servico\instalar_servico.bat
cd /d "%~dp0"
start "" http://127.0.0.1:8010
python run_server.py
