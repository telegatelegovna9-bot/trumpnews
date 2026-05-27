import subprocess
import os

os.chdir(r"C:\Users\fames_rd\Desktop\trump mode")

output = []

# Check git status
result = subprocess.run(["git", "status"], capture_output=True, text=True)
output.append("=== GIT STATUS ===")
output.append(result.stdout)
output.append(result.stderr)

# Check git log
result = subprocess.run(["git", "log", "--oneline", "-3"], capture_output=True, text=True)
output.append("\n=== GIT LOG ===")
output.append(result.stdout)
output.append(result.stderr)

# Git add
result = subprocess.run(["git", "add", "."], capture_output=True, text=True)
output.append("\n=== GIT ADD ===")
output.append(f"Exit: {result.returncode}")
output.append(result.stdout)
output.append(result.stderr)

# Git commit
result = subprocess.run(["git", "commit", "-m", "v3: API interception via Playwright"], capture_output=True, text=True)
output.append("\n=== GIT COMMIT ===")
output.append(f"Exit: {result.returncode}")
output.append(result.stdout)
output.append(result.stderr)

# Git push
result = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True, timeout=60)
output.append("\n=== GIT PUSH ===")
output.append(f"Exit: {result.returncode}")
output.append(result.stdout)
output.append(result.stderr)

# Write to file
with open(r"C:\Users\fames_rd\Desktop\trump mode\git_output.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(output))

print("Done! Check git_output.txt")
