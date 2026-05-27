import os

path = r"C:\Users\fames_rd\Desktop\trump mode\main.py"
print(f"File exists: {os.path.exists(path)}")
print(f"File size: {os.path.getsize(path)} bytes")

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Check for our debug code
if "API call:" in content:
    print("✅ Found 'API call:' in file")
else:
    print("❌ 'API call:' NOT found in file")

# Show lines around the handler
lines = content.split("\n")
for i, line in enumerate(lines):
    if "handle_response" in line:
        print(f"\nLine {i+1}: {line}")
        for j in range(i+1, min(i+15, len(lines))):
            print(f"Line {j+1}: {lines[j]}")
        break
