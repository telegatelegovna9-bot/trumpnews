import requests
import sys

urls = [
    "https://rsshub.app/truthsocial/user/realDonaldTrump",
    "https://truthsocial.com/@realDonaldTrump.atom",
]

with open("C:/Users/fames_rd/Desktop/trump mode/test_output.txt", "w") as f:
    for url in urls:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            f.write(f"{r.status_code} {url[:80]}\n")
            if r.status_code == 200:
                f.write(f"  Len: {len(r.text)}\n")
                f.write(f"  Preview: {r.text[:500]}\n")
        except Exception as e:
            f.write(f"ERR {url[:80]}: {e}\n")
