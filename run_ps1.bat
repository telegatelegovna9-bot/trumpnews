@echo off
cd /d "C:\Users\fames_rd\Desktop\trump mode"
powershell -ExecutionPolicy Bypass -File push.ps1
echo Exit code: %errorlevel%
