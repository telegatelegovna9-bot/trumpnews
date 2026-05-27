import subprocess
import os

os.chdir(r"C:\Users\fames_rd\Desktop\trump mode")

output = []

commands = [
    ["git", "status"],
    ["git", "log", "--oneline", "-3"],
    ["git", "remote", "-v"],
    ["git", "add", "."],
    ["git", "commit", "-m", "Add debug logging"],
    ["git", "push", "origin", "main"],
]

for cmd in commands:
    output.append(f"\n>>> {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output.append(f"Exit: {result.returncode}")
        if result.stdout:
            output.append(f"Stdout: {result.stdout}")
        if result.stderr:
            output.append(f"Stderr: {result.stderr}")
    except Exception as e:
        output.append(f"Error: {e}")

with open(r"C:\Users\fames_rd\Desktop\trump mode\git_output.txt", "w") as f:
    f.write("\n".join(output))

print("Done! Check git_output.txt")
