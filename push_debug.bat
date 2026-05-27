@echo off
cd /d "C:\Users\fames_rd\Desktop\trump mode"
echo === STATUS === > git_output.txt
git status >> git_output.txt 2>&1
echo. >> git_output.txt
echo === LOG === >> git_output.txt
git log --oneline -3 >> git_output.txt 2>&1
echo. >> git_output.txt
echo === ADD === >> git_output.txt
git add . >> git_output.txt 2>&1
echo. >> git_output.txt
echo === COMMIT === >> git_output.txt
git commit -m "Add debug logging" >> git_output.txt 2>&1
echo. >> git_output.txt
echo === PUSH === >> git_output.txt
git push origin main >> git_output.txt 2>&1
echo. >> git_output.txt
echo DONE >> git_output.txt
echo Output saved to git_output.txt
