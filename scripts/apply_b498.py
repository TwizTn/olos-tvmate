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

replace_once('VERSION = "0.777.b497"','VERSION = "0.777.b498"',"version")
replace_once(
    """ .tvplayerslot.mini .tvvideohit{cursor:zoom-in}
""",
    """ .tvplayerslot.mini .tvvideohit{cursor:zoom-in}
 #tvVideo{cursor:zoom-out}
 .tvplayerslot.mini #tvVideo{cursor:zoom-in}
""",
    "video cursor",
)
replace_once(
    """  const btn=slot.querySelector('.tvminbtn'),hit=slot.querySelector('.tvvideohit');
""",
    """  const btn=slot.querySelector('.tvminbtn');
""",
    "remove Live TV overlay lookup",
)
replace_once(
    """  if(hit)hit.setAttribute('aria-label',label);
""",
    "",
    "remove Live TV overlay label",
)
replace_once(
    """<video id="tvVideo" controls autoplay playsinline></video><button type="button" class="tvvideohit" aria-label="Minimize player" onclick="tvToggleMini()"></button>""",
    """<video id="tvVideo" controls autoplay playsinline onclick="tvVideoSurfaceClick(event)"></video>""",
    "native video interaction",
)
replace_once(
    """  tvSetMini(true);
}
function tvStop(){
""",
    """  tvSetMini(true);
}
function tvVideoSurfaceClick(event){
  const video=event&&event.currentTarget;
  if(!video)return;
  // Native controls occupy the bottom of the video. Preserve a generous zone
  // for play/pause, timeline, volume and option clicks.
  const rect=video.getBoundingClientRect(),controlZone=Math.min(92,Math.max(58,rect.height*.14));
  if(event.clientY>=rect.bottom-controlZone)return;
  tvToggleMini();
}
function tvStop(){
""",
    "video surface click handling",
)
replace_once(
    """    check("layout-aware pages expose an immediate on-page editor",
""",
    """    check("Live TV native controls remain hoverable and clickable",
          'onclick="tvVideoSurfaceClick(event)"' in PAGE and
          "function tvVideoSurfaceClick(event)" in PAGE and
          "controlZone=Math.min(92,Math.max(58,rect.height*.14))" in PAGE and
          "if(event.clientY>=rect.bottom-controlZone)return" in PAGE)
    check("layout-aware pages expose an immediate on-page editor",
""",
    "native controls self-test",
)

path.write_text(text,encoding="utf-8",newline="\n")
raw=path.read_bytes().replace(b"\r\n",b"\n").replace(b"\r",b"\n")
digest=hashlib.sha256(raw).hexdigest()
Path("version.txt").write_text("0.777.b498\n"+digest+"\n",encoding="utf-8",newline="\n")
print(f"b498 prepared: {len(raw)} bytes {digest}")
