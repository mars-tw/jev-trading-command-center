'use strict';
const characters=[
 ['codex','CODEX','霜甲柯基','工程 / 建造','#a6ddf4','精密護甲與程式終端，負責把方案做成可用的成果。'],
 ['claude','CLAUDE','緋墨靈貓','思考 / 審閱','#f0bf99','帶著筆記與文件，把細節讀懂，再逐一核對。'],
 ['grok','GROK','焰影獵狐','偵查 / 找碴','#efa977','敏銳的巡邏夥伴，帶著偵查裝備尋找被忽略的線索。'],
 ['agy','AGY','曜羽游隼','協作 / 傳遞','#ead896','金色羽翼與任務裝備，穿梭於不同工作線之間。'],
 ['jev','JEV','夜巡星梟','總控 / 決策','#d5c4ef','沉著的星夜守望者，整理證據並守住決策節奏。'],
 ['muse','MUSE','墨竹熊貓','創作 / 構思','#9fdbc1','帶著創作與製圖工具，把零散想法整理成新的視野。'],
 ['qwen','QWEN','紫電靈兔','資料 / 解析','#ccadef','靈活的資料夥伴，以分析裝備穿梭繁複資訊。']
];
const mode=document.getElementById('mode'),view=document.getElementById('view'),emotion=document.getElementById('emotion'),play=document.getElementById('play'),loadStatus=document.getElementById('loadStatus');
let playing=!matchMedia('(prefers-reduced-motion:reduce)').matches,frame=0,last=0,loaded=0,failed=0;
const cards=characters.map(([id,alias,name,role,tone,desc])=>{
 const card=document.createElement('article');card.className='card';card.style.setProperty('--tone',tone);card.dataset.alias=alias;
 const top=document.createElement('div');top.className='card-top';const tag=document.createElement('span');tag.className='alias';tag.textContent=alias;const job=document.createElement('span');job.className='role';job.textContent=role;top.append(tag,job);
 const stage=document.createElement('div');stage.className='stage';const canvas=document.createElement('canvas');canvas.width=canvas.height=384;canvas.setAttribute('role','img');canvas.setAttribute('aria-label',alias+' '+name+' 完整姿態素材預覽');stage.appendChild(canvas);
 const info=document.createElement('div');info.className='info';const title=document.createElement('h2');title.textContent=name;const description=document.createElement('p');description.textContent=desc;const state=document.createElement('div');state.className='state';state.textContent='讀取原畫中';
 const details=document.createElement('details');details.className='sheet';const summary=document.createElement('summary');summary.textContent='查看完整 24 格素材';const sheet=document.createElement('img');sheet.src=id+'.png';sheet.alt=alias+' 四方向、動作與表情完整透明圖集';sheet.loading='lazy';const link=document.createElement('a');link.className='asset-link';link.href=id+'.png';link.target='_blank';link.rel='noopener';link.textContent='開啟透明 PNG 原畫 ↗';details.append(summary,sheet,link);info.append(title,description,state,details);card.append(top,stage,info);document.getElementById('gallery').appendChild(card);
 const item={id,canvas,state,ready:false,image:new Image()};item.image.onload=()=>{if(item.image.naturalWidth!==1536||item.image.naturalHeight!==2304){failed++;state.textContent='圖集尺寸不符';state.classList.add('error');}else{loaded++;item.ready=true;}updateLoad();draw(item);};item.image.onerror=()=>{failed++;state.textContent='原畫讀取失敗';state.classList.add('error');updateLoad();};item.image.src=id+'.png';return item;
});
function updateLoad(){loadStatus.textContent=loaded+' / 7 角色已就緒'+(failed?' · '+failed+' 讀取失敗':' · 168 幅完整姿態');loadStatus.classList.toggle('error',failed>0);}
function draw(item){const x=item.canvas.getContext('2d');x.clearRect(0,0,384,384);if(!item.ready)return;const kind=mode.value,col=kind==='expression'?Number(emotion.value):Number(view.value),row=kind==='expression'?5:kind==='work'?[3,4,3,4][frame]:kind==='walk'?[1,0,2,0][frame]:0;x.drawImage(item.image,col*384,row*384,384,384,0,0,384,384);item.canvas.dataset.cell=col+','+row;item.state.textContent=(kind==='expression'?emotion.selectedOptions[0].textContent:mode.selectedOptions[0].textContent)+' · '+(kind==='expression'?'正面':view.selectedOptions[0].textContent)+' · 素材預覽';}
function drawAll(){view.disabled=mode.value==='expression';emotion.disabled=mode.value!=='expression';cards.forEach(draw);}
function syncPlay(){play.textContent=playing?'暫停播放':'播放動作';play.setAttribute('aria-pressed',String(playing));}
play.onclick=()=>{playing=!playing;syncPlay();};[mode,view,emotion].forEach(el=>el.onchange=()=>{frame=0;drawAll();});
function tick(t){if(playing&&!document.hidden&&t-last>=180){frame=(frame+1)%4;last=t;drawAll();}requestAnimationFrame(tick);}syncPlay();drawAll();requestAnimationFrame(tick);
window.__petGalleryReadback=()=>({loaded,failed,playing,mode:mode.value,view:Number(view.value),frame,cards:cards.map(c=>({id:c.id,ready:c.ready,cell:c.canvas.dataset.cell}))});
