@echo off
cd /d "C:\Users\fames_rd\Desktop\trump mode"
git add .
git commit -m "Fix greenlet thread error - use main thread polling"
git push origin main
echo.
echo DONE! Check Railway now.
pause
