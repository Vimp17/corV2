// Behavioral tests of the shipped CO2 controller, without a frontend framework.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag='div') { this.tag=tag; this.style={}; this.children=[]; this.listeners={}; this.value=''; this.checked=false; }
  append(...nodes) { for(const n of nodes) { if(n && n.parent) n.parent.children=n.parent.children.filter(x=>x!==n); this.children.push(n); if(n && typeof n==='object') n.parent=this; } }
  replaceChildren(...nodes) { this.children=[]; this.append(...nodes); }
  setAttribute(key,value) { this[key]=key==='checked'||key==='disabled'||key==='hidden' ? true : value; }
  addEventListener(event,fn) { (this.listeners[event] ||= []).push(fn); }
  dispatch(event) { return Promise.all((this.listeners[event]||[]).map(fn=>fn({preventDefault(){}}))); }
}
function descendants(node) { return [node,...node.children.flatMap(n=>typeof n==='object'?descendants(n):[])]; }
function control(root,label) { const node=descendants(root).find(n=>n.tag==='label' && n.children.some(x=>typeof x==='string'?x.startsWith(label):x?.textContent?.startsWith(label))); return node.children.find(n=>n.tag==='input'); }
function create() {
  const roots={wellCo2:new Element(),co2Feature:new Element()};
  const context={Node:Element,document:{createElement:t=>new Element(t),createTextNode:t=>String(t)},
    $:id=>roots[id]||descendants(roots.wellCo2).find(n=>n.id===id), state:{selected:'A'},
    ruNum:String, console, FormData:class{}, refresh:async()=>{}, api:async()=>({})};
  const html=fs.readFileSync('dashboard/index.html','utf8');
  const helper=html.slice(html.indexOf('    function el('),html.indexOf('    const headers ='));
  const controller=fs.existsSync('dashboard/co2.js')?fs.readFileSync('dashboard/co2.js','utf8'):
    html.slice(html.indexOf('    // CO₂ scenario'),html.indexOf('  </script>')).replace('    initCo2();','');
  vm.createContext(context); vm.runInContext(helper+controller,context);
  return {context,root:roots.wellCo2};
}
const detail=id=>({well_id:id,co2_enabled:true,co2_assessments:[]});
test('manual comparison drafts survive rebuilding forms for other wells',()=>{
  const {context:c}=create();const first=c.co2Form('A');
  control(first,'Температура').value='60';
  c.co2Form('B'); assert.equal(c.co2Form('A'),first);
  assert.equal(control(c.co2Form('A'),'Температура').value,'60');
});
test('pending manual comparison remains pending when its form is reused',async()=>{
  const {context:c}=create();const form=c.co2Form('A');fillNorsok(form);
  let finish;c.state.selected='B';c.api=async()=>new Promise(resolve=>{finish=resolve;});
  const request=form.dispatch('submit');
  assert.equal(descendants(c.co2Form('A')).find(n=>n.tag==='button').disabled,true);
  finish({assessment:{},duplicates:0});await request;
  assert.equal(descendants(c.co2Form('A')).find(n=>n.tag==='button').disabled,false);
});
test('measured comparison composition requires one value',async()=>{
  const {context:c}=create();const form=c.co2Form('A');fillNorsok(form);
  control(form,'Доля CO₂').value='1;2';let calls=0;c.api=async()=>{calls++;};
  await form.dispatch('submit');assert.equal(calls,0);
});
test('global result opens the submitted well after refresh even if the form changes',async()=>{
  const {context:c}=create(); const form=c.co2Form();
  control(form,'Скважина').value='QA-A'; fillNorsok(form);
  let finish; c.api=async()=>new Promise(resolve=>{finish=resolve;});
  let refreshed=false,opened; c.refresh=async()=>{refreshed=true;};c.openWell=id=>{assert.equal(refreshed,true);opened=id;};
  const pending=form.dispatch('submit'); control(form,'Скважина').value='QA-B';
  finish({assessment:{calculation_mode:'calculated',model_version:'test',scenarios:[],quality_flags:[],inputs:{}},duplicates:0});
  await pending;
  const link=descendants(form).find(n=>n.tag==='button' && n.textContent?.startsWith('Открыть скважину'));
  await link.dispatch('click'); assert.equal(opened,'QA-A');
});
test('manual comparison with a known well uses its fixed ID',async()=>{
  const {context:c}=create();const form=c.co2Form('A');
  assert.equal(descendants(form).find(n=>n.tag==='input' && n['aria-label']==='Скважина'),undefined);
  fillNorsok(form);
  let sent;c.api=async(path,options)=>{if(options){sent=JSON.parse(options.body);return {assessment:{},duplicates:0};}return {assessments:[]};};
  await form.dispatch('submit');assert.equal(sent.well_id,'A');
});
test('source metadata is optional and blank values use the manual-entry default',async()=>{
  const {context:c}=create();const form=c.co2Form('A');
  fillNorsok(form);
  control(form,'Источник входных').value='';
  let sent;c.api=async(path,options)=>{if(options){sent=JSON.parse(options.body);return {assessment:{},duplicates:0};}return {assessments:[]};};
  await form.dispatch('submit');assert.equal(sent.source,'Ручной ввод');assert.equal(sent.state_at,null);
  control(form,'Источник входных').value='Протокол лаборатории';
  await form.dispatch('submit');assert.equal(sent.source,'Протокол лаборатории');
});

test('NORSOK never submits missing pH, shear or applicability confirmations',async()=>{
  const {context:c}=create();const form=c.co2Form('A');
  control(form,'Температура').value='60';control(form,'Абсолютное').value='50';
  control(form,'Доля CO₂').value='1.5';
  let calls=0;c.api=async()=>{calls++;};await form.dispatch('submit');
  assert.equal(calls,0);
  assert.match(descendants(form).map(n=>n.textContent||'').join(' '),/pH/);
});
test('NORSOK submits measured pH and shear with explicit material and water contact',async()=>{
  const {context:c}=create();const form=c.co2Form('A');
  fillNorsok(form);
  let sent;c.api=async(path,options)=>{if(options){sent=JSON.parse(options.body);return {assessment:{},duplicates:0};}return {assessments:[]};};
  await form.dispatch('submit');
  assert.equal(sent.model_id,'active');assert.equal(sent.ph_in_situ,4);
  assert.equal(sent.wall_shear_pa,19);assert.equal(sent.material,'carbon_steel');
  assert.equal(sent.water_wetting,'water_wet');assert.equal(sent.water_cut_pct,null);
});
function fillNorsok(form){
  control(form,'Температура').value='60';control(form,'Абсолютное').value='50';
  control(form,'Доля CO₂').value='1.5';control(form,'pH при рабочих').value='4';
  control(form,'Касательное').value='19';control(form,'Подтверждаю углеродистую').checked=true;
  control(form,'Подтверждаю контакт').checked=true;
}
test('well card shows ready automatic CO2 result with read-only input provenance',()=>{
  const {context:c,root}=create();c.renderCo2({...detail('A'),co2_automatic:automatic(1.2)});
  const nodes=descendants(root);
  assert.ok(nodes.some(n=>n.textContent?.includes('Автоматический расчёт')));
  assert.ok(nodes.some(n=>n.textContent==='1.2'));
  assert.ok(nodes.some(n=>n.textContent?.includes('Подстановка администратора')));
  assert.equal(nodes.filter(n=>n.tag==='form'||n.tag==='input').length,0);
});
test('refresh replaces automatic result without a calculation request',()=>{
  const {context:c,root}=create();let requests=0;c.api=()=>{requests++;};
  c.renderCo2({...detail('A'),co2_automatic:automatic(1.2)});
  c.updateCo2Automatic('A',automatic(2.4));
  assert.ok(descendants(root).some(n=>n.textContent==='2.4'));
  assert.equal(requests,0);
});
function automatic(rate){return {automatic:true,automatic_status:'calculated',calculation_mode:'automatic',
 model_version:'norsok',model_title:'NORSOK',quality_flags:[],inputs:{temperature_c:60},
 scenarios:[{co2_mole_fraction:.015,pco2_bar:.75,rate_mm_year:rate}],defaults_used:['temperature_c'],
 input_provenance:{temperature_c:{kind:'default',value:60,reason:'Нет значения'}}};}
