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

replace_once('VERSION = "0.777.b496"','VERSION = "0.777.b497"',"version")
replace_once(
    '"Update available":"Oppdatering tilgjengelig","you have":"du har","Downloading...":"Laster ned...","Popout Player is running · click to restore":"Popout-spilleren kjører · klikk for å gjenopprette",',
    '"Update available":"Oppdatering tilgjengelig","you have":"du har","Downloading...":"Laster ned...","Popout Player is running · click to restore":"Popout-spilleren kjører · klikk for å gjenopprette","Picture-in-Picture is not supported by this browser.":"Picture-in-Picture støttes ikke av denne nettleseren.","Could not open Picture-in-Picture.":"Kunne ikke åpne Picture-in-Picture.",',
    "PiP messages",
)
replace_once(
    """function tvRestorePlayerLayout(){
""",
    """async function tvEnterPictureInPicture(){
  const video=document.getElementById('tvVideo'),slot=document.getElementById('tvPlayerSlot');
  if(!video||!video.requestPictureInPicture){toast(tr('Picture-in-Picture is not supported by this browser.'));return;}
  _tvPipRestoreMini=!!(slot&&slot.classList.contains('mini'));
  try{
    const win=document.pictureInPictureElement===video?null:await video.requestPictureInPicture();
    // Set OTVM state explicitly. Browser-native video pop-out controls may not
    // expose their state to the page, but this app-owned action always does.
    tvSetPipStatus(true);
    tvRememberPipWindow(win);
  }catch(e){toast(tr('Could not open Picture-in-Picture.'));}
}
function tvRestorePlayerLayout(){
""",
    "app-owned PiP action",
)
replace_once(
    """<div class="tvplayeractions"><button type="button" class="tvminbtn" title="Minimize player" aria-label="Minimize player" onclick="tvToggleMini()">&#8600;</button>""",
    """<div class="tvplayeractions"><button type="button" class="tvminbtn" title="Pop out player" aria-label="Pop out player" onclick="tvEnterPictureInPicture()">&#10697; Pop out</button><button type="button" class="tvminbtn" title="Minimize player" aria-label="Minimize player" onclick="tvToggleMini()">&#8600;</button>""",
    "player Pop out button",
)
replace_once(
    """          "function tvWatchPictureInPicture(video)" in PAGE and
          "function tvRestorePlayerLayout()" in PAGE and
""",
    """          "function tvWatchPictureInPicture(video)" in PAGE and
          "function tvEnterPictureInPicture()" in PAGE and
          'onclick="tvEnterPictureInPicture()"' in PAGE and
          "function tvRestorePlayerLayout()" in PAGE and
""",
    "owned PiP self-test",
)

path.write_text(text,encoding="utf-8",newline="\n")
raw=path.read_bytes().replace(b"\r\n",b"\n").replace(b"\r",b"\n")
digest=hashlib.sha256(raw).hexdigest()
Path("version.txt").write_text("0.777.b497\n"+digest+"\n",encoding="utf-8",newline="\n")
print(f"b497 prepared: {len(raw)} bytes {digest}")
