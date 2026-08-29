from pathlib import Path

path = Path('tvmate.py')
text = path.read_text(encoding='utf-8')


def replace_once(old: str, new: str, label: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'{label}: expected exactly one anchor, found {count}')
    text = text.replace(old, new, 1)


replace_once('VERSION = "0.777.b493"', 'VERSION = "0.777.b494"', 'version')

replace_once(
    ' .tvplayerslot.on{display:block}\n',
    ' .tvplayerslot.on{display:block}\n'
    ' .tvplayerslot.pipactive{position:fixed!important;left:-10000px!important;right:auto!important;top:0!important;bottom:auto!important;width:320px!important;height:180px!important;min-width:0!important;min-height:0!important;opacity:.01!important;pointer-events:none!important;overflow:hidden!important;border:0!important;box-shadow:none!important}\n'
    ' .tvplayerslot.pipactive .tvplayerbar,.tvplayerslot.pipactive .tvvideohit{display:none!important}\n'
    ' #tvPlayerSlot.pipactive #tvVideo{width:320px!important;height:180px!important;display:block!important}\n'
    ' .tvpipstatus{display:inline-flex;align-items:center;justify-content:center;min-width:min(460px,32vw);padding:7px 14px;border:1px solid #3975bf;border-radius:8px;background:#102746;color:#dcecff;font-size:12px;font-weight:700;white-space:nowrap;cursor:pointer}\n'
    ' .tvpipstatus.hide{display:none}\n'
    ' .tvpipstatus:hover{border-color:#65a0e7;background:#15335a;filter:none}\n',
    'PiP CSS',
)

replace_once(
    '  <span id="status" class="muted"></span>\n',
    '  <span id="status" class="muted"></span>\n'
    '  <button type="button" id="tvPipStatus" class="tvpipstatus hide" onclick="tvRestoreFromPip()">Popout Player is running · click to restore</button>\n',
    'header PiP status',
)

replace_once(
    "let _tvPlayRequest=0,_tvPendingSid='';\n",
    "let _tvPlayRequest=0,_tvPendingSid='';\nlet _tvPipActive=false,_tvPipRestoreMini=false;\n",
    'PiP state',
)

start = text.find('function tvPlayerGuide(){')
end = text.find('function tvToggleMini(){', start)
if start < 0 or end < 0 or end <= start:
    raise SystemExit('Live TV player block anchors not found')

new_block = r'''function tvPlayerGuide(){
  return document.querySelector('#mytvView .tvguide');
}
function tvPipVideo(){
  const video=document.getElementById('tvVideo');
  return video&&document.pictureInPictureElement===video?video:null;
}
function tvSetPipStatus(active){
  _tvPipActive=!!active;
  const slot=document.getElementById('tvPlayerSlot'),banner=document.getElementById('tvPipStatus');
  if(slot)slot.classList.toggle('pipactive',_tvPipActive);
  if(banner)banner.classList.toggle('hide',!_tvPipActive);
  if(_tvPipActive)document.body.classList.remove('tvsectionplay');
}
function tvBindPictureInPicture(video){
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
    tvSetPipStatus(false);
    const slot=document.getElementById('tvPlayerSlot');
    if(!slot||!slot.classList.contains('on'))return;
    if(!mytvView.classList.contains('hide'))tvSetMini(false);else tvSetMini(true);
  });
}
async function tvRestoreFromPip(){
  if(document.pictureInPictureElement&&document.exitPictureInPicture){
    try{await document.exitPictureInPicture();return;}catch(e){}
  }
  tvSetPipStatus(false);
  const slot=document.getElementById('tvPlayerSlot');
  if(slot&&slot.classList.contains('on')){
    if(!mytvView.classList.contains('hide'))tvSetMini(false);else tvSetMini(true);
  }
}
function tvSetMini(mini){
  const slot=document.getElementById('tvPlayerSlot'),guide=tvPlayerGuide();
  if(!slot||!slot.classList.contains('on'))return;
  if(_tvPipActive)return;
  const inLiveTv=!mytvView.classList.contains('hide');
  if(mini){
    if(slot.parentElement!==document.body)document.body.appendChild(slot);
    slot.classList.remove('sectionmax');
    slot.classList.add('mini');
  }else if(inLiveTv){
    slot.classList.remove('mini','sectionmax');
    if(guide&&slot.parentElement!==guide)guide.appendChild(slot);
  }else{
    if(slot.parentElement!==document.body)document.body.appendChild(slot);
    slot.classList.remove('mini');
    slot.classList.add('sectionmax');
  }
  const btn=slot.querySelector('.tvminbtn'),hit=slot.querySelector('.tvvideohit');
  const label=mini?'Fullscreen player':'Minimize player';
  if(btn){btn.title=label;btn.setAttribute('aria-label',label);btn.textContent=mini?'\u2196':'\u2198';}
  if(hit)hit.setAttribute('aria-label',label);
}
async function tvPlay(sid,name){
  const slot=document.getElementById('tvPlayerSlot'),guide=tvPlayerGuide();
  const sidKey=String(sid);
  if(_tvPendingSid===sidKey)return;
  _tvPendingSid=sidKey;
  const request=++_tvPlayRequest;
  const existingVideo=document.getElementById('tvVideo');
  const keepPip=!!(existingVideo&&document.pictureInPictureElement===existingVideo);
  const wasMini=slot.classList.contains('mini');
  // Single-playback rule: starting Live TV closes any open popup player.
  const pmodal=document.getElementById('playerModal');
  if(pmodal&&!pmodal.classList.contains('hide')){try{closePlayer();}catch(e){}}
  _tvPlaying=sid;
  slot.classList.add('on');
  if(!keepPip){
    if(!wasMini&&guide&&slot.parentElement!==guide)guide.appendChild(slot);
    slot.innerHTML='<div class="tvplayerbar"><span>'+esc(name||'')+'</span><div class="tvplayeractions"><button type="button" class="tvminbtn" title="Minimize player" aria-label="Minimize player" onclick="tvToggleMini()">&#8600;</button><button class="pclose" onclick="tvStop()">&times;</button></div></div><video id="tvVideo" controls autoplay playsinline></video><button type="button" class="tvvideohit" aria-label="Minimize player" onclick="tvToggleMini()"></button>';
    tvSetMini(wasMini);
  }else{
    tvSetPipStatus(true);
    const bar=slot.querySelector('.tvplayerbar span');if(bar)bar.textContent=name||'';
  }
  renderTvGuide();
  const video=keepPip?existingVideo:document.getElementById('tvVideo');
  tvBindPictureInPicture(video);
  if(window._tvPlaybackController){window._tvPlaybackController.stop();window._tvPlaybackController=null;}
  if(window._tvhls){try{window._tvhls.destroy();}catch(e){}window._tvhls=null;}
  if(window._tvmpegts){destroyMpegtsPlayer(window._tvmpegts);window._tvmpegts=null;}
  let urls;
  try{urls=await api('/api/hls?id='+encodeURIComponent(sid));if(urls.error||!urls.hls)throw new Error('stream url');}catch(e){return;}finally{if(request===_tvPlayRequest)_tvPendingSid='';}
  if(request!==_tvPlayRequest)return;
  window._tvPlaybackController=startSmartStream(video,urls,function(s){
    const bar=slot.querySelector('.tvplayerbar span');if(bar)bar.title=s||'';
  },function(h,t){window._tvhls=h;window._tvmpegts=t;});
}
'''

text = text[:start] + new_block + text[end:]

replace_once(
    "function tvStop(){\n  _tvPlayRequest++;_tvPendingSid='';\n  _tvPlaying=null;\n  document.body.classList.remove('tvsectionplay');\n",
    "function tvStop(){\n  _tvPlayRequest++;_tvPendingSid='';\n  _tvPlaying=null;\n  if(document.pictureInPictureElement&&document.exitPictureInPicture){try{document.exitPictureInPicture().catch(function(){});}catch(e){}}\n  tvSetPipStatus(false);\n  document.body.classList.remove('tvsectionplay');\n",
    'tvStop PiP cleanup',
)

required = [
    'VERSION = "0.777.b494"',
    'id="tvPipStatus"',
    "video.addEventListener('enterpictureinpicture'",
    "video.addEventListener('leavepictureinpicture'",
    'const keepPip=!!(existingVideo&&document.pictureInPictureElement===existingVideo);',
    "localStorage.setItem('tvmate_pip_size'",
]
for marker in required:
    if marker not in text:
        raise SystemExit(f'missing patched marker: {marker}')

path.write_text(text, encoding='utf-8', newline='\n')
print('b494 PiP patch applied')
