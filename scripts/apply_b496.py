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


replace_once('VERSION = "0.777.b495"', 'VERSION = "0.777.b496"', "version")
replace_once(
    """.tvplayerslot.pipactive{position:fixed!important;left:-10000px!important;right:auto!important;top:0!important;bottom:auto!important;width:320px!important;height:180px!important;min-width:0!important;min-height:0!important;opacity:.01!important;pointer-events:none!important;overflow:hidden!important;border:0!important;box-shadow:none!important}
 .tvplayerslot.pipactive .tvplayerbar,.tvplayerslot.pipactive .tvvideohit{display:none!important}
 #tvPlayerSlot.pipactive #tvVideo{width:320px!important;height:180px!important;display:block!important}""",
    """.tvplayerslot.pipactive,.tvplayerslot:has(#tvVideo:picture-in-picture){position:fixed!important;left:-10000px!important;right:auto!important;top:0!important;bottom:auto!important;width:320px!important;height:180px!important;min-width:0!important;min-height:0!important;opacity:.01!important;pointer-events:none!important;overflow:hidden!important;border:0!important;box-shadow:none!important}
 .tvplayerslot.pipactive .tvplayerbar,.tvplayerslot.pipactive .tvvideohit,.tvplayerslot:has(#tvVideo:picture-in-picture) .tvplayerbar,.tvplayerslot:has(#tvVideo:picture-in-picture) .tvvideohit{display:none!important}
 #tvPlayerSlot.pipactive #tvVideo,#tvPlayerSlot #tvVideo:picture-in-picture{width:320px!important;height:180px!important;display:block!important}""",
    "native PiP CSS fallback",
)
replace_once(
    "let _tvPipActive=false,_tvPipRestoreMini=false;",
    "let _tvPipActive=false,_tvPipRestoreMini=false,_tvPipWatchTimer=null;",
    "PiP watcher state",
)
replace_once(
    """function tvBindPictureInPicture(video){
  if(!video||video._tvmatePipBound)return;
  video._tvmatePipBound=true;
  video.addEventListener('enterpictureinpicture',function(e){
    const slot=document.getElementById('tvPlayerSlot');
    _tvPipRestoreMini=!!(slot&&slot.classList.contains('mini'));
    tvSetPipStatus(true);
    const win=e.pictureInPictureWindow;
    const remember=function(){try{localStorage.setItem('tvmate_pip_size',JSON.stringify({width:win.width,height:win.height}));}catch(err){}};
    if(win){remember();win.addEventListener('resize',remember);}
  });
  video.addEventListener('leavepictureinpicture',function(){
    tvRestorePlayerLayout();
  });
}
""",
    """function tvBindPictureInPicture(video){
  if(!video||video._tvmatePipBound)return;
  video._tvmatePipBound=true;
  video.addEventListener('enterpictureinpicture',function(e){
    tvSyncPictureInPicture(video,e.pictureInPictureWindow);
  });
  video.addEventListener('leavepictureinpicture',function(){
    tvSyncPictureInPicture(video);
  });
  tvWatchPictureInPicture(video);
}
function tvRememberPipWindow(win){
  if(!win)return;
  const remember=function(){try{localStorage.setItem('tvmate_pip_size',JSON.stringify({width:win.width,height:win.height}));}catch(err){}};
  remember();win.addEventListener('resize',remember);
}
function tvSyncPictureInPicture(video,pipWindow){
  const active=!!(video&&document.pictureInPictureElement===video);
  if(active&&!_tvPipActive){
    const slot=document.getElementById('tvPlayerSlot');
    _tvPipRestoreMini=!!(slot&&slot.classList.contains('mini'));
    tvSetPipStatus(true);
    tvRememberPipWindow(pipWindow);
  }else if(!active&&_tvPipActive){
    tvRestorePlayerLayout();
  }
}
function tvWatchPictureInPicture(video){
  if(_tvPipWatchTimer)clearInterval(_tvPipWatchTimer);
  _tvPipWatchTimer=setInterval(function(){
    if(video!==document.getElementById('tvVideo')){clearInterval(_tvPipWatchTimer);_tvPipWatchTimer=null;return;}
    tvSyncPictureInPicture(video);
  },200);
}
""",
    "resilient PiP state synchronization",
)
replace_once(
    """          'id="tvPipStatus" class="tvpipstatus hide"' in PAGE and
          "const layoutPlayback=hasPlayback&&!pipPlayback" in PAGE and
          "if(!keepMytv&&layoutPlayback)" in PAGE and
          "function tvRestorePlayerLayout()" in PAGE and
""",
    """          'id="tvPipStatus" class="tvpipstatus hide"' in PAGE and
          ":has(#tvVideo:picture-in-picture)" in PAGE and
          "const layoutPlayback=hasPlayback&&!pipPlayback" in PAGE and
          "if(!keepMytv&&layoutPlayback)" in PAGE and
          "function tvSyncPictureInPicture(video,pipWindow)" in PAGE and
          "function tvWatchPictureInPicture(video)" in PAGE and
          "function tvRestorePlayerLayout()" in PAGE and
""",
    "PiP resilience self-test",
)

path.write_text(text, encoding="utf-8", newline="\n")
raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
digest = hashlib.sha256(raw).hexdigest()
Path("version.txt").write_text("0.777.b496\n" + digest + "\n", encoding="utf-8", newline="\n")
print(f"b496 prepared: {len(raw)} bytes {digest}")
