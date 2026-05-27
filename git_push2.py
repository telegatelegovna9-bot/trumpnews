import subprocess
import os

os.chdir(r"C:\Users\fames_rd\Desktop\trump mode")

# Git add
result = subprocess.run(["git", "add", "."], capture_output=True, text=True)
print(f"ADD: {result.returncode}")

# Git commit
result = subprocess.run(["git", "commit", "-m", "Debug: log all API calls to find correct filter"], capture_output=True, text=True)
print(f"COMMIT: {result.returncode}")
print(f"  stdout: {result.stdout}")
print(f"  stderr: {result.stderr}")

# Git push
result = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True, timeout=60)
print(f"PUSH: {result.returncode}")
print(f"  stdout: {result.stdout}")
print(f"  stderr: {result.stderr}")
