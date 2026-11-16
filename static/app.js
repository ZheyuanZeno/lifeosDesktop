const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const templates = {
 journal:{name:'每日记录',type:'journal',icon:'▱',view:'list',fields:[]},
 health:{name:'健康记录',type:'activity',icon:'♡',view:'table',fields:[{id:'duration',name:'时长（分钟）',type:'number'}]},
 shopping:{name:'购物记录',type:'expense',icon:'▤',view:'table',fields:[{id:'merchant',name:'商家',type:'text'}]},
 plans:{name:'计划',type:'task',icon:'☑',view:'list',fields:[]},
 custom:{name:'新的模块',type:'note',icon:'▦',view:'table',fields:[]}
};
let state, page='board', query='', editingModule=null, moduleFields=[], editingRecord=null, editingInbox=null, attachments=[], inboxAttachments=[], busy=false, uploading=false, timer, pomodoroCompleting=false;
let openModuleTabs=[], activeWorkspaceTab='board';
try{const savedTabs=JSON.parse(localStorage.getItem('lifeos-module-tabs')||'null');if(savedTabs&&Array.isArray(savedTabs.open)){openModuleTabs=savedTabs.open;activeWorkspaceTab=savedTabs.active||'board';}}catch{}
const expanded = new Set();
const today = () => {const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;};
const modules = () => state.modules.filter(m=>!m.archived);
const moduleFor = id => state.modules.find(m=>m.id===id);
const moduleRecords = id => state.records.filter(r=>r.module_id===id&&!r.deleted).sort((a,b)=>moduleFor(id).kind==='plans'?a.date.localeCompare(b.date):b.date.localeCompare(a.date));
const clone = obj => structuredClone(obj);
function toast(text){clearTimeout(timer);$('#toast').textContent=text;$('#toast').hidden=false;timer=setTimeout(()=>$('#toast').hidden=true,4500);}
async function api(path,payload){const res=await fetch(path,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-LifeOS-Token':state.token},body:JSON.stringify(payload)});const body=await res.json();if(!res.ok)throw new Error(body.error||'操作未完成');return body;}
async function refresh(){state=await api('/api/state');render();}
async function mutate(path,payload){const result=await api(path,{...payload,expected_revision:state.updated_at});await refresh();return result;}
function renderInboxAttachments(){
 $('#inbox-attachments').innerHTML=inboxAttachments.map((a,i)=>`<div class="attachment-item"><a href="/attachments/${esc(a.file)}" target="_blank" rel="noopener">${esc(a.name)}</a><button type="button" class="quiet" data-remove-inbox-attachment="${i}">移除</button></div>`).join('');
}
function renderInbox(){
 const pending=(state.inbox||[]).filter(item=>!item.processed_at).sort((a,b)=>b.created_at.localeCompare(a.created_at));
 $('#inbox-count').textContent=pending.length?`${pending.length} 条`:'';
 $('#inbox-list').innerHTML=pending.length?pending.map(item=>`<article class="inbox-item ${editingInbox?.id===item.id?'editing':''}"><div class="inbox-item-head"><time>${esc(new Date(item.created_at).toLocaleString())}</time><div class="inbox-item-actions"><button type="button" data-edit-inbox="${esc(item.id)}">编辑</button><button type="button" class="inbox-delete" data-delete-inbox="${esc(item.id)}" aria-label="删除这条收件箱内容" title="删除">删除</button></div></div>${item.body?`<p>${esc(item.body)}</p>`:''}${item.attachments.length?`<div class="inbox-files">附件：${item.attachments.map(a=>`<a href="/attachments/${esc(a.file)}" target="_blank" rel="noopener">${esc(a.name)}</a>`).join(' · ')}</div>`:''}</article>`).join(''):'<p class="inbox-empty">暂时没有等待整理的内容。</p>';
}
function resetInboxEditor(){editingInbox=null;inboxAttachments=[];$('#inbox-form').reset();$('#inbox-heading').textContent='原始收件箱';$('#save-inbox').textContent='存入收件箱';$('#cancel-inbox-edit').hidden=true;$('#inbox-error').textContent='';renderInboxAttachments();renderInbox();}
function editInbox(id){const item=(state.inbox||[]).find(value=>value.id===id&&!value.processed_at);if(!item)return;editingInbox=clone(item);inboxAttachments=clone(item.attachments);$('#inbox-body').value=item.body;$('#inbox-heading').textContent='编辑收件箱条目';$('#save-inbox').textContent='保存修改';$('#cancel-inbox-edit').hidden=false;$('#inbox-error').textContent='';renderInboxAttachments();renderInbox();$('#inbox-body').focus();}
function openInbox(){resetInboxEditor();$('#inbox-dialog').showModal();}
function renderThemeChoices(){
 $$('.theme-card').forEach(card=>{
  const selected=card.dataset.theme===(state.theme||'light');
  card.classList.toggle('selected',selected);
  card.setAttribute('aria-pressed',String(selected));
 });
}
function openTheme(){renderThemeChoices();$('#theme-dialog').showModal();}
function activePomodoro(){return (state?.pomodoros||[]).find(item=>item.status==='running'||item.status==='paused');}
function pomodoroElapsed(item){
 let seconds=item.elapsed_seconds||0;
 if(item.status==='running'&&item.phase_started_at)seconds+=Math.max(0,Math.floor((Date.now()-Date.parse(item.phase_started_at))/1000));
 return item.mode==='countup'?seconds:Math.min(item.duration_minutes*60,seconds);
}
function clockText(seconds){seconds=Math.max(0,Math.ceil(seconds));return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;}
function durationText(seconds){const minutes=Math.floor(seconds/60),rest=seconds%60;return `${minutes} 分${rest?` ${rest} 秒`:''}`;}
function renderPomodoro(){
 if(!state)return;
 const active=activePomodoro(),button=$('#canvas-pomodoro'),badge=$('#pomodoro-badge');
 button.classList.toggle('active',!!active);badge.hidden=!active;badge.textContent=active?.status==='paused'?'Ⅱ':'';
 $('#pomodoro-save-label').hidden=!active;
 const inputLabel=$('#pomodoro-label'),inputDuration=$('#pomodoro-duration'),start=$('#pomodoro-start'),pause=$('#pomodoro-pause'),finish=$('#pomodoro-finish'),mode=$('#pomodoro-mode');
 if(active)mode.value=active.mode||'countdown';
 mode.disabled=!!active;
 $('.pomodoro-clock').hidden=!active;
 $('#timer-segments').classList.toggle('countup',mode.value==='countup');
 $$('[data-timer-mode]').forEach(b=>{b.disabled=!!active;b.setAttribute('aria-pressed',String(b.dataset.timerMode===mode.value));});
 $('.pomodoro-settings').classList.toggle('countup',mode.value==='countup');
 const countup=mode.value==='countup';
 inputDuration.parentElement.hidden=countup;
 if(active){
  const elapsed=pomodoroElapsed(active),remaining=active.duration_minutes*60-elapsed;
  $('#pomodoro-time').textContent=clockText(countup?elapsed:remaining);$('#pomodoro-status').textContent=active.status==='paused'?'已暂停':active.label||'正在专注';
  if(!inputLabel.dataset.dirty)inputLabel.value=active.label;inputDuration.value=active.duration_minutes;inputLabel.disabled=false;inputDuration.disabled=true;
  start.hidden=true;pause.hidden=false;finish.hidden=false;pause.textContent=active.status==='paused'?'继续':'暂停';
  if(!countup&&active.status==='running'&&remaining<=0&&!pomodoroCompleting){pomodoroCompleting=true;refresh().then(()=>toast('本次专注已完成')).catch(error=>toast(error.message)).finally(()=>pomodoroCompleting=false);}
 }else{
  $('#pomodoro-time').textContent=countup?'00:00':clockText(Math.max(1,Number(inputDuration.value)||25)*60);$('#pomodoro-status').textContent=countup?'自由专注，结束时保存时长':'准备专注';
  inputLabel.disabled=false;inputDuration.disabled=false;start.hidden=false;pause.hidden=true;finish.hidden=true;
 }
}
function openPomodoro(){delete $('#pomodoro-label').dataset.dirty;renderPomodoro();$('#pomodoro-error').textContent='';$('#pomodoro-dialog').showModal();}
async function runPomodoro(action){
 if(busy)return;busy=true;$('#pomodoro-error').textContent='';
 try{const active=activePomodoro();const payload={action};if(action==='start'){payload.label=$('#pomodoro-label').value;payload.mode=$('#pomodoro-mode').value;payload.duration_minutes=Number($('#pomodoro-duration').value);}else payload.id=active?.id;if(action==='rename')payload.label=$('#pomodoro-label').value;await mutate('/api/pomodoro',payload);delete $('#pomodoro-label').dataset.dirty;renderPomodoro();if(action==='rename')toast('专注内容已保存');}
 catch(error){$('#pomodoro-error').textContent=error.message;}
 finally{busy=false;}
}
$('#pomodoro-label').addEventListener('input',()=>{$('#pomodoro-label').dataset.dirty='true';});
$('#pomodoro-save-label').addEventListener('click',()=>runPomodoro('rename'));
$('#pomodoro-mode').addEventListener('change',()=>{if(!activePomodoro())renderPomodoro();});
$('#pomodoro-start').addEventListener('click',()=>runPomodoro('start'));
$('#pomodoro-pause').addEventListener('click',()=>runPomodoro(activePomodoro()?.status==='paused'?'resume':'pause'));
$('#pomodoro-finish').addEventListener('click',()=>runPomodoro('finish'));
$('#pomodoro-duration').addEventListener('input',()=>{if(!activePomodoro())renderPomodoro();});
function taskLabel(r){return `<div class="check-title ${r.status==='done'?'done':''}"><input class="task-check" type="checkbox" data-check="${esc(r.id)}" aria-label="计划：${esc(r.title)}" ${r.status==='done'?'checked':''}><button type="button" class="row-title" data-record="${esc(r.id)}">${esc(r.title)}</button></div>`;}
function titleCell(r){return r.type==='task'?taskLabel(r):`<button type="button" class="row-title" data-record="${esc(r.id)}">${esc(r.title)}</button>`;}
function valueCell(value){return value?esc(value):'<span class="muted">—</span>';}
function amount(r){return r.amount===null?'<span class="muted">待补</span>':`${esc(r.amount)} ${esc(r.currency)}`;}
function renderTable(m,rows){return `<div class="table-wrap"><table class="database-table"><thead><tr><th>名称</th><th>日期</th>${m.kind==='shopping'?'<th>金额</th>':''}${m.fields.map(f=>`<th>${esc(f.name)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr><td>${titleCell(r)}</td><td class="date-cell">${esc(r.date)}</td>${m.kind==='shopping'?`<td>${amount(r)}</td>`:''}${m.fields.map(f=>`<td>${valueCell(r.properties[f.id])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;}
function renderList(m,rows){return `<div class="database-list">${rows.map(r=>`<div class="list-row"><div class="list-topline">${titleCell(r)}<time>${esc(r.date)}</time></div>${r.body?`<p class="list-summary">${esc(r.body)}</p>`:''}${m.kind==='shopping'?`<div class="list-properties">${amount(r)}</div>`:''}${m.fields.some(f=>r.properties[f.id])?`<p class="list-properties">${m.fields.filter(f=>r.properties[f.id]).map(f=>`${esc(f.name)}：${esc(r.properties[f.id])}`).join(' · ')}</p>`:''}</div>`).join('')}</div>`;}
function layoutFor(m){const i=modules().findIndex(x=>x.id===m.id),p=m.layout||{};return {x:p.x??70+(i%2)*530,y:p.y??85+Math.floor(i/2)*350,width:p.width??460,height:p.height??300,z:Number.isFinite(p.z)&&p.z>0?p.z:i+1};}
function focusHistoryModule(m,focused=false){
 const p=layoutFor(m),items=(state.pomodoros||[]).filter(x=>!x.deleted&&['completed','stopped'].includes(x.status)).sort((a,b)=>b.started_at.localeCompare(a.started_at));
 const style=focused?'':`left:${p.x}px;top:${p.y}px;width:${p.width}px;height:${p.height}px;z-index:${p.z}`;
 return `<section class="database-block ${focused?'focused-module':''}" id="${esc(m.id)}" data-module="${esc(m.id)}" style="${style}"><div class="module-header" tabindex="0" aria-label="拖动${esc(m.name)}，也可用方向键移动"><h2><button class="module-tab-link" data-open-module-tab="${esc(m.id)}">${esc(m.name)}</button></h2><div class="module-tools"><button class="module-add" data-open-pomodoro aria-label="开始专注">+</button><details class="module-menu"><summary aria-label="专注历史的设置与属性">...</summary><div class="menu-content"><button data-edit-module="${esc(m.id)}">设置与属性</button><button data-page="data">数据与导出</button><button data-archive-module="${esc(m.id)}">删除模块</button></div></details></div></div><div class="module-content">${items.length?items.map(x=>`<button class="focus-history-row" data-edit-focus="${esc(x.id)}"><strong>${esc(x.label||'未命名专注')}</strong><span>${esc(new Date(x.started_at).toLocaleString())} · ${esc(durationText(x.elapsed_seconds))} · ${x.status==='completed'?'已完成':'提前结束'}</span></button>`).join(''):'<p class="inbox-empty">完成专注后，历史记录会自动显示在这里。</p>'}</div>${focused?'':`<span class="resize-handle" data-resize="${esc(m.id)}" aria-label="调整${esc(m.name)}大小"></span>`}</section>`;
}
function database(m,focused=false){if(m.id==='module-focus-history')return focusHistoryModule(m,focused);const rows=moduleRecords(m.id);const p=layoutFor(m);const style=focused?'':`left:${p.x}px;top:${p.y}px;width:${p.width}px;height:${p.height}px;z-index:${p.z}`;return `<section class="database-block ${focused?'focused-module':''}" id="${esc(m.id)}" data-module="${esc(m.id)}" style="${style}"><div class="module-header" tabindex="0" aria-label="${focused?esc(m.name):`拖动${esc(m.name)}，也可用方向键移动`}"><h2><button type="button" class="module-tab-link" data-open-module-tab="${esc(m.id)}">${esc(m.name)}</button></h2><div class="module-tools"><button class="module-add" data-new-record="${esc(m.id)}" aria-label="向${esc(m.name)}添加记录">+</button><details class="module-menu"><summary aria-label="${esc(m.name)}的设置与属性">...</summary><div class="menu-content"><button data-edit-module="${esc(m.id)}">设置与属性</button><button data-view-module="${esc(m.id)}">切换${m.view==='table'?'列表':'表格'}</button><div class="menu-divider"></div><a href="/api/export?module=${esc(m.id)}&format=zip">导出此模块</a><a href="/api/export?module=${esc(m.id)}&format=csv">导出 CSV</a><button data-page="data">白板数据与导出</button><button data-archive-module="${esc(m.id)}">删除模块</button></div></details></div></div><div class="module-content">${m.view==='table'?renderTable(m,rows):renderList(m,rows)}</div>${focused?'':`<span class="resize-handle" data-resize="${esc(m.id)}" aria-label="调整${esc(m.name)}大小"></span>`}</section>`;}
function boardView(){return modules().map(m=>database(m)).join('');}
function rememberModuleTabs(){try{localStorage.setItem('lifeos-module-tabs',JSON.stringify({open:openModuleTabs,active:activeWorkspaceTab}));}catch{}}
function syncModuleTabs(){const available=new Set(modules().map(m=>m.id));openModuleTabs=openModuleTabs.filter(id=>available.has(id));if(activeWorkspaceTab!=='board'&&!available.has(activeWorkspaceTab))activeWorkspaceTab='board';rememberModuleTabs();}
function renderModuleTabs(){syncModuleTabs();$('#module-tabs').innerHTML=`<button type="button" class="workspace-tab board-tab ${activeWorkspaceTab==='board'?'active':''}" data-workspace-tab="board">白板</button>${openModuleTabs.map(id=>{const m=moduleFor(id);return `<div class="workspace-tab ${activeWorkspaceTab===id?'active':''}"><button type="button" data-workspace-tab="${esc(id)}">${esc(m.name)}</button><button type="button" class="tab-close" data-close-module-tab="${esc(id)}" aria-label="关闭${esc(m.name)}标签">×</button></div>`;}).join('')}`;}
function openModuleTab(id){if(!moduleFor(id)||moduleFor(id).archived)return;if(!openModuleTabs.includes(id))openModuleTabs.push(id);activeWorkspaceTab=id;rememberModuleTabs();render();}
function switchWorkspaceTab(id){activeWorkspaceTab=id==='board'||openModuleTabs.includes(id)?id:'board';rememberModuleTabs();render();}
function closeModuleTab(id){const index=openModuleTabs.indexOf(id);openModuleTabs=openModuleTabs.filter(value=>value!==id);if(activeWorkspaceTab===id)activeWorkspaceTab=openModuleTabs[index-1]||openModuleTabs[index]||'board';rememberModuleTabs();render();}
function exportsView(){return `<h1 class="page-heading">数据与导出</h1><div class="export-row"><div><h3>带走整个白板</h3><p>包含模块、属性、记录和原始附件。也包含 HTML 阅读版、Markdown 和各模块的 CSV。</p></div><a class="primary" href="/api/export?format=zip">完整备份 ZIP ↓</a></div><div class="export-row"><div><h3>在其他地方阅读</h3><p>文字和图片包含在一个文件里，用浏览器即可打开。</p></div><a class="quiet" href="/api/export?format=html">阅读版 HTML ↓</a></div><div class="export-row"><div><h3>原始数据</h3><p>导出模块定义和全部记录。JSON 不包含附件内容。</p></div><a class="quiet" href="/api/export?format=json">导出 JSON ↓</a></div><div class="export-row"><div><h3>导入备份或整理结果</h3><p>可以新增模块并填入记录，也能恢复已有备份。</p></div><button class="quiet" id="open-import">导入 ↗</button></div><div class="export-row"><div><h3>撤销与恢复</h3><p>最近一次新增、修改或整批导入可以撤销。移除的模块和记录可在回收站找回。</p></div><button class="quiet" id="undo" ${state.can_undo?'':'disabled'}>撤销上一次修改</button></div><button class="quiet" data-page="trash">打开回收站</button><p class="data-info">资料目录：${esc(state.data_path)}<br>完整记录存于本机 SQLite 数据库；JSON 与 Markdown 是同步生成的导出副本。</p><button class="quiet" data-page="board">← 回到白板</button>`;}
function trashView(){const archived=state.modules.filter(m=>m.archived);const deleted=state.records.filter(r=>r.deleted);return `<h1 class="page-heading">回收站</h1><p class="hint">这里的内容可以恢复，原始数据仍然保留。</p>${archived.map(m=>`<div class="trash-row"><div>${esc(m.name)} <small>模块</small></div><button class="quiet" data-restore-module="${esc(m.id)}">恢复模块</button></div>`).join('')}${deleted.map(r=>`<div class="trash-row"><div>${esc(r.title)}<br><small>${esc(r.date)} · ${esc(moduleFor(r.module_id)?.name)}</small></div><button class="quiet" data-restore-record="${esc(r.id)}">恢复记录</button></div>`).join('')}${!archived.length&&!deleted.length?'<p class="hint">回收站是空的。</p>':''}<button class="quiet" data-page="board">← 回到白板</button>`;}
function render(){
 document.documentElement.dataset.theme=state.theme||'light';syncModuleTabs();const focused=activeWorkspaceTab==='board'?null:moduleFor(activeWorkspaceTab);document.body.classList.toggle('module-focus',!!focused);$('#workspace').innerHTML=focused?database(focused,true):boardView();if(focused)$('#workspace').style.transform='none';else applyCamera();renderModuleTabs();
 $$('.database-block').forEach(block=>block.dataset.kind=moduleFor(block.dataset.module).kind);
 $$('#module-tabs [data-workspace-tab]').forEach(button=>{const m=moduleFor(button.dataset.workspaceTab);if(m)button.parentElement.dataset.kind=m.kind;});
 renderPomodoro();
 if(page!=='board')$('#utility-content').innerHTML=page==='data'?exportsView():trashView();
}
function changePage(value){page=value;render();if(value==='board')$('#utility-dialog').close();else if(!$('#utility-dialog').open)$('#utility-dialog').showModal();}
function renderFields(){ $('#field-list').innerHTML=moduleFields.map((f,i)=>`<div class="property-row"><input aria-label="属性 ${i+1} 名称" data-field-index="${i}" data-key="name" value="${esc(f.name)}" required maxlength="60"><select aria-label="属性 ${i+1} 类型" data-field-index="${i}" data-key="type">${[['text','文字'],['number','数字'],['date','日期']].map(([v,n])=>`<option value="${v}" ${f.type===v?'selected':''}>${n}</option>`).join('')}</select><button type="button" class="icon-button" data-remove-field="${i}" aria-label="移除属性 ${i+1}">×</button></div>`).join('');}
function openModule(id){editingModule=id?clone(moduleFor(id)):null;const m=editingModule||{...templates.journal,kind:'journal'};$('#module-kind').value=m.kind;$('#module-kind').disabled=!!id;$('#module-name').value=m.name;$('#module-view').value=m.view;moduleFields=clone(m.fields);renderFields();$('#module-heading').textContent=id?'模块设置':'添加一个模块';$('#save-module').textContent=id?'保存':'创建模块';$('#module-error').textContent='';$('#module-dialog').showModal();}
$('#module-kind').addEventListener('change',()=>{const preset=templates[$('#module-kind').value];const names=Object.values(templates).map(x=>x.name);if(!$('#module-name').value||names.includes($('#module-name').value))$('#module-name').value=preset.name;$('#module-view').value=preset.view;moduleFields=clone(preset.fields);renderFields();});
$('#add-field').addEventListener('click',()=>{if(moduleFields.length>=30)return toast('每个模块最多 30 个属性');moduleFields.push({id:'field-'+crypto.randomUUID().replaceAll('-',''),name:'新属性',type:'text'});renderFields();});
$('#field-list').addEventListener('input',e=>{const i=e.target.dataset.fieldIndex;if(i!==undefined)moduleFields[Number(i)][e.target.dataset.key]=e.target.value;});
$('#module-form').addEventListener('submit',async e=>{e.preventDefault();if(busy)return;busy=true;$('#save-module').disabled=true;try{const module={...editingModule,layout:editingModule?.layout||newModuleLayout(),kind:$('#module-kind').value,name:$('#module-name').value,view:$('#module-view').value,fields:moduleFields};await mutate('/api/module',{module});$('#module-dialog').close();changePage('board');toast(editingModule?'模块已更新':'模块已添加，可以开始记录了');}catch(e){$('#module-error').textContent=e.message;}finally{busy=false;$('#save-module').disabled=false;}});
function activeRecordType(){return editingRecord?.type||templates[moduleFor($('#record-module').value).kind].type;}
function recordProperties(values={}){const m=moduleFor($('#record-module').value);const type=activeRecordType();$('#status-label').hidden=type!=='task';$('#amount-label').hidden=type!=='expense';$('#currency-label').hidden=type!=='expense';$('#custom-properties').innerHTML=m.fields.map(f=>`<label>${esc(f.name)}<input data-property="${esc(f.id)}" type="${f.type==='date'?'date':'text'}" ${f.type==='number'?'inputmode="decimal"':''} value="${esc(values[f.id]??'')}" placeholder="留空也可以"></label>`).join('');}
function renderAttachments(){ $('#record-attachments').innerHTML=attachments.map((a,i)=>`<div class="attachment-item"><a href="/attachments/${esc(a.file)}" target="_blank" rel="noopener">${esc(a.name)}</a><button class="quiet" type="button" data-remove-attachment="${i}">移除关联</button></div>${/\.(png|jpg|jpeg|webp|gif)$/i.test(a.file)?`<img class="attachment-image" alt="${esc(a.name)}" src="/attachments/${esc(a.file)}">`:''}`).join('');}
function relatedView(r){if(!r)return '';const linked=state.records.filter(x=>x.source_id===r.id&&!x.deleted&&!moduleFor(x.module_id)?.archived);const source=state.records.find(x=>x.id===r.source_id);return `${linked.length?`<section class="related"><h3>关联的记录</h3>${linked.map(x=>`<div class="related-row"><small>${esc(moduleFor(x.module_id)?.name)} · ${esc(x.date)}</small><div>${x.type==='task'?taskLabel(x):titleCell(x)}</div></div>`).join('')}</section>`:''}${source?`<section class="related"><small class="hint">来自日记</small><div><button type="button" class="row-title" data-record="${esc(source.id)}">${esc(source.title)}</button></div></section>`:''}`;}
function openRecord(id,moduleId){if(uploading)return toast('附件还在上传，请稍候');editingRecord=id?clone(state.records.find(r=>r.id===id)):null;const m=moduleFor(editingRecord?.module_id||moduleId);if(!m)return;const type=editingRecord?.type||templates[m.kind].type;const date=today();const r=editingRecord||{module_id:m.id,title:type==='journal'?`${Number(date.slice(5,7))}.${Number(date.slice(8))} 日记`:'',date,body:'',status:type==='task'?'todo':'done',amount:'',currency:''};const form=$('#record-form');form.reset();$('#record-module').innerHTML=state.modules.filter(x=>!x.archived||x.id===r.module_id).map(x=>`<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('');['module_id','title','date','body','status','amount','currency'].forEach(key=>form.elements[key].value=r[key]??'');recordProperties(r.properties);attachments=clone(r.attachments||[]);renderAttachments();$('#record-heading').textContent=m.name;$('#delete-record').hidden=!editingRecord;$('#related-records').innerHTML=relatedView(editingRecord);$('#record-error').textContent='';if(!$('#record-dialog').open)$('#record-dialog').showModal();}
$('#record-module').addEventListener('change',()=>recordProperties(editingRecord?.properties));
async function saveRecord(record,expected){return mutate('/api/save',{record,expected_updated_at:expected});}
$('#record-form').addEventListener('submit',async e=>{e.preventDefault();if(busy||uploading)return;busy=true;$('#save-record').disabled=true;try{const v=Object.fromEntries(new FormData(e.target));const type=activeRecordType();const properties={...editingRecord?.properties};$$('[data-property]').forEach(input=>properties[input.dataset.property]=input.value);const r={...editingRecord,...v,type,properties,attachments};if(type!=='expense'){r.amount=null;r.currency='';}if(type!=='task')r.status='done';await saveRecord(r,editingRecord?.updated_at);$('#record-dialog').close();toast('已保存');}catch(e){$('#record-error').textContent=e.message;}finally{busy=false;$('#save-record').disabled=false;}});
function base64(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(Error('文件读取失败'));reader.readAsDataURL(file);});}
$('#inbox-file').addEventListener('change',async e=>{uploading=true;$('#save-inbox').disabled=true;try{for(const file of e.target.files){if(file.size>10*1024*1024)throw Error('单个附件最多 10MB');if(inboxAttachments.length>=30)throw Error('每条原始记录最多 30 个附件');inboxAttachments.push(await api('/api/attachment',{name:file.name,data:await base64(file)}));renderInboxAttachments();}}catch(e){$('#inbox-error').textContent=e.message;}finally{uploading=false;$('#save-inbox').disabled=false;e.target.value='';}});
$('#inbox-form').addEventListener('submit',async e=>{e.preventDefault();if(busy||uploading)return;busy=true;$('#save-inbox').disabled=true;const wasEditing=!!editingInbox;try{await mutate('/api/inbox',{id:editingInbox?.id,body:$('#inbox-body').value,attachments:inboxAttachments});resetInboxEditor();toast(wasEditing?'收件箱条目已更新':'已存入原始收件箱');}catch(e){$('#inbox-error').textContent=e.message;}finally{busy=false;$('#save-inbox').disabled=false;}});
$('#attachment-input').addEventListener('change',async e=>{uploading=true;$('#save-record').disabled=true;try{for(const file of e.target.files){if(file.size>10*1024*1024)throw Error('单个附件最多 10MB');if(attachments.length>=30)throw Error('每条记录最多 30 个附件');attachments.push(await api('/api/attachment',{name:file.name,data:await base64(file)}));renderAttachments();}}catch(e){$('#record-error').textContent=e.message;}finally{uploading=false;$('#save-record').disabled=false;e.target.value='';}});
$('#delete-record').addEventListener('click',async()=>{if(busy||!editingRecord)return;busy=true;try{await saveRecord({...editingRecord,deleted:true},editingRecord.updated_at);$('#record-dialog').close();toast('已移到回收站');}catch(e){$('#record-error').textContent=e.message;}finally{busy=false;}});
async function changeModule(module){await mutate('/api/module',{module});}
document.addEventListener('click',async e=>{
 const button=e.target.closest('button');const link=e.target.closest('[data-module-link]');if(link){page='board';query='';$('#search-area').hidden=true;render();return;}if(!button)return;
 if(button.hasAttribute('data-close')){if(uploading)return toast('附件还在上传，请稍候');button.closest('dialog').close();return;}
 if(button.hasAttribute('data-open-inbox')){openInbox();return;}
 if(button.dataset.editFocus){openFocusEdit(button.dataset.editFocus);return;}
 if(button.hasAttribute('data-open-pomodoro')){openPomodoro();return;}
 if(button.hasAttribute('data-open-theme')){openTheme();return;}
 if(button.hasAttribute('data-add-module')){openModule();return;}
 if(button.dataset.openModuleTab){openModuleTab(button.dataset.openModuleTab);return;}
 if(button.dataset.workspaceTab){switchWorkspaceTab(button.dataset.workspaceTab);return;}
 if(button.dataset.closeModuleTab){closeModuleTab(button.dataset.closeModuleTab);return;}
 if(button.dataset.editInbox){editInbox(button.dataset.editInbox);return;}
 if(button.id==='cancel-inbox-edit'){resetInboxEditor();return;}
 if(button.dataset.removeInboxAttachment!==undefined){inboxAttachments.splice(Number(button.dataset.removeInboxAttachment),1);renderInboxAttachments();return;}
 if(button.dataset.theme){
  if(busy)return;busy=true;button.disabled=true;
  try{await mutate('/api/theme',{theme:button.dataset.theme});renderThemeChoices();toast('主题已切换');}
  catch(error){toast(error.message);}
  finally{busy=false;button.disabled=false;}
  return;
 }
 if(button.dataset.deleteInbox){if(busy)return;busy=true;button.disabled=true;try{const id=button.dataset.deleteInbox;await mutate('/api/inbox/delete',{id});if(editingInbox?.id===id)resetInboxEditor();else renderInbox();toast('已删除收件箱内容');}catch(error){toast(error.message);renderInbox();}finally{busy=false;}return;}
 if(button.dataset.page){if($('#module-dialog').open)$('#module-dialog').close();changePage(button.dataset.page);return;}
 if(button.dataset.editModule){openModule(button.dataset.editModule);return;}
 if(button.dataset.newRecord){openRecord(null,button.dataset.newRecord);return;}
 if(button.dataset.record){openRecord(button.dataset.record);return;}
 if(button.dataset.removeField!==undefined){moduleFields.splice(Number(button.dataset.removeField),1);renderFields();return;}
 if(button.dataset.removeAttachment!==undefined){attachments.splice(Number(button.dataset.removeAttachment),1);renderAttachments();return;}
 if(button.dataset.expand){expanded.has(button.dataset.expand)?expanded.delete(button.dataset.expand):expanded.add(button.dataset.expand);render();return;}
 if(button.id==='open-import'){$('#import-error').textContent='';$('#import-dialog').showModal();return;}
 if(busy)return;
 const action=button.dataset.viewModule||button.dataset.move||button.dataset.archiveModule||button.dataset.restoreModule||button.dataset.restoreRecord||button.id==='undo';if(!action)return;busy=true;
 try{
  if(button.dataset.viewModule){const m=moduleFor(button.dataset.viewModule);await changeModule({...m,view:m.view==='table'?'list':'table'});}
  if(button.dataset.move){const id=button.dataset.move;const active=modules();const i=active.findIndex(m=>m.id===id);const target=active[i+Number(button.dataset.direction)];if(target){const ids=state.modules.map(m=>m.id);const a=ids.indexOf(id),b=ids.indexOf(target.id);[ids[a],ids[b]]=[ids[b],ids[a]];await mutate('/api/reorder',{ids});}}
  if(button.dataset.archiveModule){await changeModule({...moduleFor(button.dataset.archiveModule),archived:true});toast('模块已删除，可在回收站恢复');}
  if(button.dataset.restoreModule){await changeModule({...moduleFor(button.dataset.restoreModule),archived:false});toast('模块已恢复到白板');}
  if(button.dataset.restoreRecord){const r=state.records.find(r=>r.id===button.dataset.restoreRecord);await saveRecord({...r,deleted:false},r.updated_at);toast(moduleFor(r.module_id).archived?'记录已恢复；所属模块仍在回收站':'记录已恢复');}
  if(button.id==='undo'){await mutate('/api/undo',{});toast('已撤销上一次修改');}
 }catch(e){toast(e.message);}finally{busy=false;}
});
document.addEventListener('change',async e=>{const id=e.target.dataset.check;if(!id)return;if(busy){e.target.checked=!e.target.checked;return;}busy=true;const r=state.records.find(x=>x.id===id);e.target.disabled=true;try{await saveRecord({...r,status:e.target.checked?'done':'todo'},r.updated_at);if($('#record-dialog').open&&editingRecord)$('#related-records').innerHTML=relatedView(editingRecord);toast('计划状态已保存');}catch(error){toast(error.message);render();}finally{busy=false;}});
$('#run-import').addEventListener('click',async()=>{if(busy)return;busy=true;$('#run-import').disabled=true;try{const payload={overwrite:$('#overwrite').checked};const file=$('#import-file').files[0];if(file){if(file.size>30*1024*1024)throw Error('导入文件最多 30MB');if(file.name.toLowerCase().endsWith('.zip'))payload.archive=await base64(file);else payload.text=await file.text();}else payload.text=$('#import-text').value;const r=await mutate('/api/import',payload);$('#import-dialog').close();$('#import-text').value='';$('#import-file').value='';$('#overwrite').checked=false;changePage('board');toast(`已导入：${r.modules_added} 个新模块，${r.added} 条新记录；更新 ${r.updated}，跳过 ${r.skipped}`);}catch(e){$('#import-error').textContent=e.message;}finally{busy=false;$('#run-import').disabled=false;}});
$('#copy-prompt').addEventListener('click',async()=>{const prompt=`请将我的生活记录整理为 LifeOS JSON，只输出 {"schema_version":2,"modules":[],"records":[]}。当前模块定义如下：${JSON.stringify(state.modules)}。
使用现有模块的 id 填入每条记录的 module_id；确实需要新类别时，在 modules 中创建模块定义 {id:唯一英文编号,name:名称,kind:journal/health/shopping/plans/custom,view:list或table,fields:[],archived:false}。只返回新模块即可。常用模板：日记 journal，健康 health，购物 shopping，计划 plans，自定义 custom。
记录字段：id（稳定唯一英文编号，修订时沿用）、module_id、type（journal/activity/expense/task/note）、date（YYYY-MM-DD）、title、body、status（todo/done）、amount（字符串或null）、currency、source_id、attachments（空数组）、properties（属性编号对应文字值）。健康记录可填写模块定义中已有的 duration，购物可填写已有的 merchant；不知道就留空。
日记完整保留原文。默认只提炼明确的未来计划与已完成体育锻炼；起床、领证、普通购物、做饭、聚餐等只留在原文。用户明确提供购物小票或要求记账时才创建购物记录；若没有购物模块，同时创建它。计划与锻炼通过 source_id 关联原始日记。不要猜金额、币种、时间或时长。金额未知为null；有金额但币种未知请先询问。明天相对于原文归属日期。缺失的日期请先询问。不要将计划当成已完成。
我的记录：`;try{await navigator.clipboard.writeText(prompt);toast('已复制，粘贴到 ChatGPT 后附上你的记录');}catch(e){$('#import-error').textContent='未能复制，请允许浏览器读取剪贴板后重试。';}});
async function init(){try{state=await api('/api/state');if(!state.modules)throw Error('白板正在升级，请稍后刷新。');render();}catch(e){toast(e.message+'；请确认本地 LifeOS 已启动');}}
setInterval(()=>{if(state)renderPomodoro();},1000);


// Camera coordinates are independent of module world coordinates: no fixed canvas edge.
let camera={x:0,y:0,scale:1}, gesture=null, spaceHeld=false, suppressClick=false;
try { const saved=JSON.parse(localStorage.getItem('lifeos-camera')||'null');if(saved&&[saved.x,saved.y,saved.scale].every(Number.isFinite)&&saved.scale>=0.25&&saved.scale<=2)camera=saved;} catch {}
function rememberCamera(){try{localStorage.setItem('lifeos-camera',JSON.stringify(camera));}catch{}}
function applyCamera(){$('#workspace').style.transform=`translate(${camera.x}px,${camera.y}px) scale(${camera.scale})`;}
function newModuleLayout(){
 const base={x:(window.innerWidth/2-camera.x)/camera.scale-230,y:(window.innerHeight/2-camera.y)/camera.scale-120,width:460,height:300,z:Math.max(0,...modules().map(m=>layoutFor(m).z))+1};
 let tries=0;while(tries<40&&modules().some(m=>{const p=layoutFor(m);return Math.abs(p.x-base.x)<45&&Math.abs(p.y-base.y)<45;})){base.x+=45;base.y+=45;tries++;}return base;
}
const viewport=$('#viewport');
viewport.addEventListener('pointerdown',e=>{
 if(document.body.classList.contains('module-focus'))return;
 if(e.button!==0&&e.button!==1)return;if(e.target.closest('button,input,select,textarea,a,summary,.menu-content'))return;
 const block=e.target.closest('.database-block');const header=e.target.closest('.module-header');const resize=e.target.closest('.resize-handle');
 if(block&&!header&&!resize&&!spaceHeld&&e.button!==1)return;if(busy||gesture)return;
 const drag=header&&!spaceHeld&&e.button===0;const resizeMode=resize&&e.button===0;const module=(drag||resizeMode)?clone(moduleFor(block.dataset.module)):null;
 const layout=module?{...layoutFor(module),z:Math.max(0,...modules().map(m=>layoutFor(m).z))+1}:null;
 gesture={pointer:e.pointerId,startX:e.clientX,startY:e.clientY,lastX:e.clientX,lastY:e.clientY,mode:resizeMode?'resize':drag?'module':'pan',module,layout,originalZ:module?layoutFor(module).z:null,element:(drag||resizeMode)?block:null,camera:{...camera},moved:false};
 if(drag||resizeMode){block.style.zIndex=layout.z;block.classList.add('dragging');}viewport.setPointerCapture(e.pointerId);viewport.classList.add('moving');e.preventDefault();
});
viewport.addEventListener('pointermove',e=>{
 if(!gesture||gesture.pointer!==e.pointerId)return;const g=gesture;const dx=e.clientX-g.startX,dy=e.clientY-g.startY;
 if(Math.abs(dx)+Math.abs(dy)>4)g.moved=true;g.lastX=e.clientX;g.lastY=e.clientY;
 if(g.mode==='pan'){camera.x=g.camera.x+dx;camera.y=g.camera.y+dy;applyCamera();}
 else if(g.mode==='module') {g.element.style.left=(g.layout.x+dx/camera.scale)+'px';g.element.style.top=(g.layout.y+dy/camera.scale)+'px';}
 else {g.element.style.width=Math.max(280,Math.min(1600,g.layout.width+dx/camera.scale))+'px';g.element.style.height=Math.max(160,Math.min(1200,g.layout.height+dy/camera.scale))+'px';}
});
async function finishGesture(e,cancel=false){
 if(!gesture||gesture.pointer!==e.pointerId)return;const g=gesture;gesture=null;viewport.classList.remove('moving');
 if(viewport.hasPointerCapture(e.pointerId))viewport.releasePointerCapture(e.pointerId);
 if(g.element){g.element.classList.remove('dragging');g.element.style.zIndex=g.moved?g.layout.z:g.originalZ;}
 if(cancel){if(g.mode==='pan'){camera=g.camera;applyCamera();}else render();return;}
 if(g.mode==='pan'){rememberCamera();return;}if(!g.moved)return;
 suppressClick=true;setTimeout(()=>suppressClick=false,0);busy=true;
 const layout={...g.layout};
 if(g.mode==='module'){layout.x=g.layout.x+(g.lastX-g.startX)/camera.scale;layout.y=g.layout.y+(g.lastY-g.startY)/camera.scale;}
 else {layout.width=Math.max(280,Math.min(1600,g.layout.width+(g.lastX-g.startX)/camera.scale));layout.height=Math.max(160,Math.min(1200,g.layout.height+(g.lastY-g.startY)/camera.scale));}
 try{
  await changeModule({...g.module,layout});
  // Older running servers do not yet retain `z`; moving the module to the end
  // keeps the same stacking behaviour until the app is restarted.
  if(moduleFor(g.module.id)?.layout?.z===undefined){const ids=state.modules.map(m=>m.id),i=ids.indexOf(g.module.id);if(i>=0&&i<ids.length-1){ids.push(ids.splice(i,1)[0]);await mutate('/api/reorder',{ids});}}
 }catch(error){toast('位置未保存：'+error.message);render();}finally{busy=false;}
}
viewport.addEventListener('pointerup',e=>finishGesture(e));viewport.addEventListener('pointercancel',e=>finishGesture(e,true));
viewport.addEventListener('click',e=>{if(suppressClick){e.preventDefault();e.stopPropagation();}},true);
viewport.addEventListener('wheel',e=>{
 if(document.body.classList.contains('module-focus'))return;
 if(gesture)return;const content=e.target.closest('.module-content');
 if(content&&!e.ctrlKey&&!e.metaKey&&!e.shiftKey&&content.scrollHeight>content.clientHeight&&((e.deltaY<0&&content.scrollTop>0)||(e.deltaY>0&&content.scrollTop+content.clientHeight<content.scrollHeight-1)))return;
 e.preventDefault();const unit=e.deltaMode===1?16:e.deltaMode===2?window.innerHeight:1;
 if(e.ctrlKey||e.metaKey){const old=camera.scale;camera.scale=Math.min(2,Math.max(.25,old*Math.exp(-e.deltaY*.002)));camera.x=e.clientX-(e.clientX-camera.x)*camera.scale/old;camera.y=e.clientY-(e.clientY-camera.y)*camera.scale/old;}
 else{camera.x-=(e.shiftKey&&e.deltaX===0?e.deltaY:e.deltaX)*unit;camera.y-=(e.shiftKey&&e.deltaX===0?0:e.deltaY)*unit;}applyCamera();rememberCamera();
},{passive:false});
document.addEventListener('keydown',e=>{
 if(e.target.closest('input,textarea,select,dialog'))return;
 if(e.code==='Space'){spaceHeld=true;viewport.classList.add('space-pan');e.preventDefault();}
 if(e.key==='Escape'&&page==='board'){if(activeWorkspaceTab!=='board')switchWorkspaceTab('board');else{camera={x:0,y:0,scale:1};applyCamera();rememberCamera();}}
 const header=e.target.closest('.module-header');if(activeWorkspaceTab==='board'&&header&&['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)&&!busy){e.preventDefault();const m=moduleFor(header.closest('[data-module]').dataset.module),p=layoutFor(m),step=e.shiftKey?50:10;busy=true;changeModule({...m,layout:{...p,x:p.x+(e.key==='ArrowLeft'?-step:e.key==='ArrowRight'?step:0),y:p.y+(e.key==='ArrowUp'?-step:e.key==='ArrowDown'?step:0)}}).catch(error=>toast(error.message)).finally(()=>busy=false);}
});
document.addEventListener('keyup',e=>{if(e.code==='Space'){spaceHeld=false;viewport.classList.remove('space-pan');}});
window.addEventListener('blur',()=>{spaceHeld=false;viewport.classList.remove('space-pan');});
init();

let editingFocusId=null;
function openFocusEdit(id){const item=state.pomodoros.find(x=>x.id===id);editingFocusId=id;$('#focus-edit-label').value=item.label;$('#focus-edit-minutes').value=Math.floor(item.elapsed_seconds/60);$('#focus-edit-seconds').value=item.elapsed_seconds%60;$('#focus-edit-error').textContent='';$('#focus-edit-dialog').showModal();}
$$('[data-timer-mode]').forEach(b=>b.addEventListener('click',()=>{if(activePomodoro())return;$('#pomodoro-mode').value=b.dataset.timerMode;renderPomodoro();}));
async function saveFocusEdit(action){if(busy)return;busy=true;try{await mutate('/api/pomodoro',{action,id:editingFocusId,label:$('#focus-edit-label').value,elapsed_seconds:Number($('#focus-edit-minutes').value)*60+Number($('#focus-edit-seconds').value)});$('#focus-edit-dialog').close();toast(action==='delete_history'?'已删除记录，可在数据与导出中撤销':'专注记录已更新');}catch(e){$('#focus-edit-error').textContent=e.message;}finally{busy=false;}}
$('#focus-edit-form').addEventListener('submit',e=>{e.preventDefault();saveFocusEdit('update_history');});
$('#focus-delete').addEventListener('click',()=>saveFocusEdit('delete_history'));
