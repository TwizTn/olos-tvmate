from pathlib import Path
import hashlib

path = Path("tvmate.py")
text = path.read_text(encoding="utf-8")


def replace_once(old, new, label):
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one anchor, found {count}")
    text = text.replace(old, new, 1)


replace_once('VERSION = "0.777.b494"', 'VERSION = "0.777.b495"', "version")
replace_once(
    ".tvpipstatus{display:inline-flex;align-items:center;justify-content:center;min-width:min(460px,32vw);",
    ".tvpipstatus{display:inline-flex;align-items:center;justify-content:center;flex:1 1 360px;max-width:620px;min-width:min(460px,32vw);",
    "wide PiP status",
)
replace_once(
    'id="tvPipStatus" class="tvpipstatus hide" onclick="tvRestoreFromPip()">Popout Player is running · click to restore</button>',
    'id="tvPipStatus" class="tvpipstatus hide" onclick="tvRestoreFromPip()" data-i18n="Popout Player is running · click to restore">Popout Player is running · click to restore</button>',
    "PiP status translation marker",
)
replace_once(
    '"Update available":"Oppdatering tilgjengelig","you have":"du har","Downloading...":"Laster ned...",',
    '"Update available":"Oppdatering tilgjengelig","you have":"du har","Downloading...":"Laster ned...","Popout Player is running · click to restore":"Popout-spilleren kjører · klikk for å gjenopprette",',
    "PiP Norwegian translation",
)
replace_once(
    """  const hasPlayback=!!(hasTvPlayback||hasPopupPlayback);
  const leavingLiveTv=!!(!keepMytv&&hasTvPlayback&&!mytvView.classList.contains('hide'));
""",
    """  const hasPlayback=!!(hasTvPlayback||hasPopupPlayback);
  // Picture-in-Picture is represented by the restore control in the header,
  // not by an in-page player. Keep every section at its normal full width.
  const pipPlayback=!!(_tvPipActive||tvPipVideo());
  const layoutPlayback=hasPlayback&&!pipPlayback;
  const leavingLiveTv=!!(!keepMytv&&hasTvPlayback&&!pipPlayback&&!mytvView.classList.contains('hide'));
""",
    "normal PiP page layout",
)
replace_once("if(!keepMytv&&hasPlayback){", "if(!keepMytv&&layoutPlayback){", "PiP layout gate")
replace_once(
    """  video.addEventListener('leavepictureinpicture',function(){
    tvSetPipStatus(false);
    const slot=document.getElementById('tvPlayerSlot');
    if(!slot||!slot.classList.contains('on'))return;
    if(!mytvView.classList.contains('hide'))tvSetMini(false);else tvSetMini(true);
  });
}
async function tvRestoreFromPip(){
""",
    """  video.addEventListener('leavepictureinpicture',function(){
    tvRestorePlayerLayout();
  });
}
function tvRestorePlayerLayout(){
  const slot=document.getElementById('tvPlayerSlot');
  const restoreMini=!!(_tvPipRestoreMini||mytvView.classList.contains('hide'));
  tvSetPipStatus(false);
  _tvPipRestoreMini=false;
  if(slot&&slot.classList.contains('on'))tvSetMini(restoreMini);
}
async function tvRestoreFromPip(){
""",
    "PiP restore layout helper",
)
replace_once(
    """  tvSetPipStatus(false);
  const slot=document.getElementById('tvPlayerSlot');
  if(slot&&slot.classList.contains('on')){
    if(!mytvView.classList.contains('hide'))tvSetMini(false);else tvSetMini(true);
  }
}
function tvSetMini(mini){
""",
    """  tvRestorePlayerLayout();
}
function tvSetMini(mini){
""",
    "PiP restore action",
)
replace_once(
    """    check("layout-aware pages expose an immediate on-page editor",
""",
    """    check("PiP uses a header restore control without shrinking app pages",
          'id="tvPipStatus" class="tvpipstatus hide"' in PAGE and
          "const layoutPlayback=hasPlayback&&!pipPlayback" in PAGE and
          "if(!keepMytv&&layoutPlayback)" in PAGE and
          "function tvRestorePlayerLayout()" in PAGE and
          "const restoreMini=!!(_tvPipRestoreMini||mytvView.classList.contains('hide'))" in PAGE)
    check("layout-aware pages expose an immediate on-page editor",
""",
    "PiP self-test",
)

path.write_text(text, encoding="utf-8", newline="\n")
raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
digest = hashlib.sha256(raw).hexdigest()
Path("version.txt").write_text("0.777.b495\n" + digest + "\n", encoding="utf-8", newline="\n")
print(f"b495 prepared: {len(raw)} bytes {digest}")
