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
function control(root,label) { const node=descendants(root).find(n=>n.tag==='label' && String(n.children[0]).startsWith(label)); return node.children.find(n=>n.tag==='input'); }
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
test('CO2 input and expanded form survive refresh and switching wells',()=>{
  const {context:c,root}=create(); c.renderCo2(detail('A'));
  control(root,'Температура').value='60'; descendants(root).find(n=>n.tag==='details').open=true;
  c.renderCo2(detail('A')); assert.equal(control(root,'Температура').value,'60');
  assert.equal(descendants(root).find(n=>n.tag==='details').open,true);
  c.state.selected='B'; c.renderCo2(detail('B')); assert.equal(control(root,'Температура').value,'');
  control(root,'Температура').value='70'; c.state.selected='A'; c.renderCo2(detail('A'));
  assert.equal(control(root,'Температура').value,'60');
});
test('pending CO2 request survives redraw and cannot replace another well history',async()=>{
  const {context:c,root}=create(); c.renderCo2(detail('A'));
  let finish; const calls=[]; c.api=async(path)=>{calls.push(path); return new Promise(resolve=>{finish=resolve;});};
  const form=descendants(root).find(n=>n.tag==='form');
  control(root,'Температура').value='60'; control(root,'Абсолютное').value='50';
  const request=form.dispatch('submit');
  c.renderCo2(detail('A')); assert.equal(descendants(root).find(n=>n.tag==='button').disabled,true);
  c.state.selected='B'; c.renderCo2(detail('B'));
  finish({assessment:{calculation_mode:'calculated',model_version:'test',scenarios:[],quality_flags:[],inputs:{}},duplicates:0});
  await request;
  assert.deepEqual(calls,['/api/v1/co2/calculate']);
  assert.equal(control(root,'Скважина').value,'B');
});
test('measured composition asks for one value and validates it before sending',async()=>{
  const {context:c,root}=create();c.renderCo2(detail('A'));
  const measured=descendants(root).find(n=>n.tag==='label' && n.children.some(x=>typeof x==='string' && x.includes('Измеренная доля'))).children.find(n=>n.tag==='input');
  measured.checked=true;await measured.dispatch('change');
  const fraction=control(root,'Доля CO₂');assert.equal(fraction.value,'');
  fraction.value='1;2';control(root,'Температура').value='60';control(root,'Абсолютное').value='50';
  let calls=0;c.api=async()=>{calls++;return {};};
  await descendants(root).find(n=>n.tag==='form').dispatch('submit');
  assert.equal(calls,0);assert.match(descendants(root).filter(n=>n.tag==='span').map(n=>n.textContent||'').join(' '),/одно значение/);
});
test('global result opens the submitted well after refresh even if the form changes',async()=>{
  const {context:c}=create(); const form=c.co2Form();
  control(form,'Скважина').value='QA-A'; control(form,'Температура').value='60'; control(form,'Абсолютное').value='50';
  let finish; c.api=async()=>new Promise(resolve=>{finish=resolve;});
  let refreshed=false,opened; c.refresh=async()=>{refreshed=true;};c.openWell=id=>{assert.equal(refreshed,true);opened=id;};
  const pending=form.dispatch('submit'); control(form,'Скважина').value='QA-B';
  finish({assessment:{calculation_mode:'calculated',model_version:'test',scenarios:[],quality_flags:[],inputs:{}},duplicates:0});
  await pending;
  const link=descendants(form).find(n=>n.tag==='button' && n.textContent?.startsWith('Открыть скважину'));
  await link.dispatch('click'); assert.equal(opened,'QA-A');
});
