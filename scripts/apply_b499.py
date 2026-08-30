from pathlib import Path
import hashlib

path=Path("tvmate.py")
text=path.read_text(encoding="utf-8")

def replace_once(old,new,label):
    global text
    count=text.count(old)
    if count!=1:
        raise SystemExit(f"{label}: expected one anchor, found {count}")
    text=text.replace(old,new,1)

replace_once('VERSION = "0.777.b498"','VERSION = "0.777.b499"',"version")
replace_once(
    """ .tvminbtn:hover{border-color:#6d86a8;filter:none}
""",
    """ .tvminbtn:hover{border-color:#6d86a8;filter:none}
 .tvpipicon{display:block;width:15px;height:15px}
""",
    "PiP icon style",
)
replace_once(
    """<button type="button" class="tvminbtn" title="Pop out / PiP layout" aria-label="Pop out / PiP layout" onclick="tvEnterPictureInPicture()">&#10697; PiP layout / Pop out</button>""",
    """<button type="button" class="tvminbtn" title="Pop out / PiP layout" aria-label="Pop out / PiP layout" onclick="tvEnterPictureInPicture()"><svg class="tvpipicon" viewBox="0 0 24 24" aria-hidden="true"><rect x="2.5" y="4.5" width="19" height="15" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><rect x="12" y="11" width="7" height="5.5" rx="1" fill="currentColor"/></svg></button>""",
    "PiP icon button",
)
replace_once(
    """    check("layout-aware pages expose an immediate on-page editor",
""",
    """    check("Live TV PiP control uses a distinct picture-in-picture icon",
          'class="tvpipicon"' in PAGE and
          '<rect x="12" y="11" width="7" height="5.5"' in PAGE and
          "&#10697; PiP layout / Pop out" not in PAGE)
    check("layout-aware pages expose an immediate on-page editor",
""",
    "PiP icon self-test",
)

path.write_text(text,encoding="utf-8",newline="\n")
raw=path.read_bytes().replace(b"\r\n",b"\n").replace(b"\r",b"\n")
digest=hashlib.sha256(raw).hexdigest()
Path("version.txt").write_text("0.777.b499\n"+digest+"\n",encoding="utf-8",newline="\n")
print(f"b499 prepared: {len(raw)} bytes {digest}")
