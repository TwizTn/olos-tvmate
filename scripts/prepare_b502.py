from pathlib import Path
import hashlib
path=Path("tvmate.py")
raw=path.read_bytes().replace(b"\r\n",b"\n").replace(b"\r",b"\n")
path.write_bytes(raw)
digest=hashlib.sha256(raw).hexdigest()
Path("version.txt").write_text("0.777.b502\n"+digest+"\n",encoding="utf-8",newline="\n")
print(f"b502 prepared: {len(raw)} bytes {digest}")
