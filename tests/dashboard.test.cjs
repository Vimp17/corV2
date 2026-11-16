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
const detail=(rate=1.2)=>({well_id:'A',co2_enabled:true,co2_automatic:{automatic_status:'calculated',
 input_hash:String(rate),scenarios:[{co2_mole_fraction:.015,pco2_bar:.75,rate_mm_year:rate}],
 quality_flags:['Комментарий, который не должен отображаться'],model_version:'technical-hash',
 input_provenance:{temperature_c:{kind:'default',value:60,reason:'Не показывать причину'},
 ph_in_situ:{kind:'source',value:5,source_id:'KHIMIK'}}}});
test('CO2 card is collapsed and shows result in its summary',()=>{
 const {context:c,root}=create();c.renderCo2(detail());
 const panel=descendants(root).find(n=>n.tag==='details');
 assert.equal(Boolean(panel.open),false);
 assert.match(descendants(panel).find(n=>n.tag==='summary').textContent,/1.2 мм\/год/);
});
test('expanded content contains only inputs, their source and results',()=>{
 const {context:c,root}=create();c.renderCo2(detail());
 const nodes=descendants(root);const text=nodes.map(n=>n.textContent||'').join(' ');
 assert.match(text,/Температура/);assert.match(text,/KHIMIK/);assert.match(text,/Настройки/);
 assert.doesNotMatch(text,/Комментарий|причину|technical-hash|JSON|Ограничения/);
 assert.equal(nodes.filter(n=>['form','input','button','pre','p','ul'].includes(n.tag)).length,0);
});
test('automatic update keeps the panel expanded',()=>{
 const {context:c,root}=create();c.renderCo2(detail());
 const panel=descendants(root).find(n=>n.tag==='details');panel.open=true;
 c.updateCo2Automatic('A',detail(2.4).co2_automatic);
 assert.equal(descendants(root).find(n=>n.tag==='details'),panel);
 assert.equal(panel.open,true);
 assert.match(descendants(panel).find(n=>n.tag==='summary').textContent,/2.4 мм\/год/);
});
test('standalone CO2 forms and navigation are absent from the shipped dashboard',()=>{
 const html=fs.readFileSync('dashboard/index.html','utf8');
 assert.doesNotMatch(html,/id="co2Feature"|id="co2Nav"|initCo2\(/);
});
