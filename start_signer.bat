@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [xhs] starting signer on 127.0.0.1:18080 ...
node xhs_signer_server.js 18080
pause
