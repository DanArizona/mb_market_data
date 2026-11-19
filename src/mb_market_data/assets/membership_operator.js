'use strict';
const el=id=>document.getElementById(id);
let csrf=null, view=null, proposal=null, latestRequest=null, refreshing=false, selected='', renderedKey='';
const names={uni:'UNI',focus:'FOCUS',hot:'HOT',outside:'OUTSIDE UNI'};
const currency=v=>v==null?'Unavailable':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:4}).format(v);
const integer=v=>v==null?'Unavailable':new Intl.NumberFormat('en-US').format(v);
const clock=v=>v?new Date(v).toLocaleString('en-US',{timeZone:'America/New_York',hour12:false,year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'} )+' ET':'Not recorded';

async function request(path,opts={}){
  const res=await fetch(path,{cache:'no-store',...opts});
  let data;
  try{data=await res.json();}catch(_){throw Error('Local operator response could not be read.');}
  if(!res.ok)throw Error(data.error||`Operator response ${res.status}`);
  return data;
}
async function post(path,payload){
  return request(path,{method:'POST',headers:{'Content-Type':'application/json','X-MB-Operator-CSRF':csrf},body:JSON.stringify(payload)});
}
function failure(message){el('page-error').textContent=message;}
function clearProposal(){proposal=null;el('review').hidden=true;}

function list(tier,items){
  const filter=el(tier+'-filter').value.trim().toUpperCase();
  const found=items.filter(s=>s.includes(filter));
  const root=el(tier+'-list');root.replaceChildren();
  if(!found.length){const p=document.createElement('div');p.className='empty';p.textContent=items.length?'No matches':'No members';root.append(p);return;}
  const fragment=document.createDocumentFragment();
  for(const symbol of found){
    const button=document.createElement('button');button.type='button';button.className='symbol-row'+(symbol===selected?' selected':'');
    button.textContent=symbol;button.setAttribute('aria-label','Inspect '+symbol);
    button.onclick=()=>{selected=symbol;el('symbol').value=symbol;clearProposal();renderLists();refresh();};
    fragment.append(button);
  }
  root.append(fragment);
}
function renderLists(){
  if(!view)return;
  const signature=JSON.stringify([view.effective_revision,view.tiers,selected,
    el('uni-filter').value,el('focus-filter').value,el('hot-filter').value]);
  if(signature===renderedKey)return;
  renderedKey=signature;
  for(const tier of ['uni','focus','hot']){
    el(tier+'-count').textContent=integer(view.counts[tier]);list(tier,view.tiers[tier]);
  }
}
function setDetails(info){
  const rank=info?.tier||'outside';const badge=el('selected-tier');
  badge.className='badge '+(rank==='outside'?'':rank);badge.textContent=names[rank]||rank;
  const root=el('detail');root.replaceChildren();root.className='details muted';
  if(!info){root.textContent='Select a member above or enter a symbol.';return;}
  const fields=[['Name',info.description],['Saved quote status',info.status],['Last',currency(info.last)],
    ['Previous close',currency(info.close)],['Open',currency(info.open)],
    ['Day range',info.low!=null&&info.high!=null?`${currency(info.low)} – ${currency(info.high)}`:null],
    ['Volume',integer(info.volume)],['Security status',info.security_status],
    ['Estimated market cap',currency(info.estimated_market_cap)],['Latest channel',info.channel?.toUpperCase()],
    ['Individual quote time',clock(info.quote_time_utc)],['Last trade time',clock(info.last_trade_time_utc)],
    ['API response received',clock(info.api_response_utc)],['Saved acquisition completed',clock(info.recorded_at_utc)]];
  if(!info.quote_available){root.textContent='No saved quote observation for this symbol at the current session time.';return;}
  root.className='details details-grid';
  for(const [label,value] of fields){
    const d=document.createElement('div');d.className='detail';const small=document.createElement('small');small.textContent=label;
    const strong=document.createElement('strong');strong.textContent=value==null||value===''?'Unavailable':String(value);
    d.append(small,strong);root.append(d);
  }
}
function renderSnapshot(v){
  view=v;
  el('session-date').textContent=v.session_date;
  el('session-phase').textContent=v.phase+(v.final?' · FINAL REPORT':'');
  el('revision').textContent=`Effective r${v.effective_revision??'—'} · Latest published r${v.published_revision??'—'} · ${clock(v.as_of_utc)}`;
  const gate=el('gate');
  gate.className='gate'+(!v.eligible?' blocked':v.pending_revision?' pending':'');
  gate.textContent=!v.eligible?'CONTROLS DISABLED · '+v.eligibility_message:
    v.pending_revision?`WAIT FOR EFFECTIVE REVISION r${v.pending_revision.revision} · ${clock(v.pending_revision.effective_at_utc)}`:
    'CONTROLS AVAILABLE · '+v.eligibility_message;
  el('preview').disabled=!v.eligible||!!v.pending_revision||!v.effective_revision||!selected;
  renderLists();setDetails(v.selected);
}
async function refresh(){
  if(refreshing)return;refreshing=true;
  try{
    const query=selected?'?symbol='+encodeURIComponent(selected):'';
    renderSnapshot(await request('/snapshot'+query));
    failure('');
  }catch(e){failure('VIEW UNAVAILABLE: '+e.message+' — no new action should be inferred.');
    el('preview').disabled=true;el('gate').className='gate blocked';el('gate').textContent='CONTROL STATUS UNKNOWN · Submission disabled.';
  }finally{refreshing=false;}
}
async function previewAction(){
  clearProposal();failure('');
  selected=el('symbol').value.trim().toUpperCase();
  if(!selected){failure('Select or enter a symbol.');return;}
  try{
    const result=await post('/preview',{symbol:selected,action:el('action').value,target:el('target').value});
    proposal=result;
    el('review-text').textContent=`${result.symbol}: ${names[result.before]} → ${names[result.after]}. `+
      `Action: ${result.action.toUpperCase()} to ${names[result.target]}; based on published revision r${result.based_on_revision}.`;
    el('review-note').textContent=result.note+' Confirmation expires '+clock(result.expires_at_utc)+'.';
    el('review').hidden=false;
  }catch(e){failure('PREVIEW DENIED: '+e.message);}
}
async function confirmAction(){
  if(!proposal)return;
  const token=proposal.proposal_id;el('confirm').disabled=true;failure('');
  try{
    const result=await post('/confirm',{proposal_id:token});
    latestRequest=result.request.id;clearProposal();
    showReceipt('Request queued: '+latestRequest+' · Waiting for the membership worker.','pending');
    await pollReceipt();await refresh();
  }catch(e){failure('CONFIRMATION NOT ACCEPTED: '+e.message);
    showReceipt('No confirmed result. Check the worker inbox before considering another request.','bad');
    clearProposal();
  }finally{el('confirm').disabled=false;}
}
function showReceipt(message,kind=''){el('receipt').textContent=message;el('receipt').className='receipt '+kind;}
async function pollReceipt(){
  if(!latestRequest)return;
  try{
    const value=await request('/result?id='+encodeURIComponent(latestRequest));
    const status=value.status;
    const detail=(value.message?' · '+value.message:'')+(value.effective_at?' · Effective at '+clock(value.effective_at):'');
    showReceipt(`Command ${latestRequest} · ${status}${detail}`,
      status==='EFFECTIVE_AS_PUBLISHED'?'good':status==='FAILED'||status==='REJECTED'||status==='NOT_FOUND'?'bad':'pending');
    if(status!=='PENDING'&&status!=='PUBLISHED_PENDING_EFFECTIVE')latestRequest=null;
  }catch(e){showReceipt('Receipt temporarily unavailable; do not resubmit blindly. '+e.message,'bad');}
}
function refreshTargets(){
  const action=el('action').value;const target=el('target');const old=target.value;
  target.replaceChildren(...(action==='demote'?['focus','uni']:['focus','hot','uni']).map(t=>new Option(names[t],t)));
  if([...target.options].some(o=>o.value===old))target.value=old;
  clearProposal();
}
async function boot(){
  for(const tier of ['uni','focus','hot'])el(tier+'-filter').addEventListener('input',()=>{renderedKey='';renderLists();});
  let debounce;
  el('symbol').addEventListener('input',()=>{selected=el('symbol').value.trim().toUpperCase();clearProposal();
    clearTimeout(debounce);debounce=setTimeout(refresh,260);});
  el('action').onchange=refreshTargets;el('target').onchange=clearProposal;
  el('preview').onclick=previewAction;el('confirm').onclick=confirmAction;el('cancel').onclick=clearProposal;
  refreshTargets();
  try{csrf=(await request('/config')).csrf;await refresh();}
  catch(e){failure('Cannot connect to local operator: '+e.message);el('preview').disabled=true;}
  setInterval(()=>{refresh();pollReceipt();},3500);
}
boot();
