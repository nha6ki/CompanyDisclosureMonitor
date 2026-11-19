// Financial amounts in supporting evidence are in millions of US dollars.
const metricNames = [
  ['Revenue', '売上高'], ['Diluted EPS', '希薄化後EPS'], ['Operating income', '営業利益'],
  ['Operating margin', '営業利益率'], ['Revenue growth (YoY)', '売上高前年同期比'],
  ['Operating cash flow', '営業キャッシュフロー'], ['Capital expenditures', '設備投資額'],
  ['Free cash flow', 'フリーキャッシュフロー']
];
const capitalScope = {
  NVDA: ['Includes purchases of intangible assets.', '無形資産の取得を含みます。'],
  TSLA: ['The scope follows the purchases reported in the source row for each period.', '対象範囲は各期の原文に記載された購入項目に従います。'],
  AMZN: ['Gross purchases of property and equipment, before proceeds and incentives.', '有形固定資産の購入支出額です。売却収入やインセンティブの受取額は差し引いていません。']
};
function preferredInput(d,key){
  return d.financials?.release[key] ?? d.financials?.report[key] ?? null;
}
function cashPair(d){
  for(const src of [d.financials?.release,d.financials?.report])
    if(src?.cash && src.capex) return [src.cash,src.capex];
  return [null,null];
}
function ratio(n,d){return n==null || d==null || d===0 ? null : n/d;}
function financialValue(d,i){
  if(i===0)return rev(d);
  if(i===1)return d.facts[1].value==null ? null : Number(d.facts[1].value);
  const e=d.financials;
  if(!e)return null;
  const inc=preferredIncome(d);
  if(i===2)return inc==null ? null : inc.value/1000;
  if(i===3 || i===4){
    const side=i===3 ? incomeStatement(d) : growthStatement(d);
    const fraction=side && (i===3 ? ratio(side.operating?.value,side.revenue?.value) : ratio(side.revenue?.value,side.priorRevenue?.value));
    return fraction==null ? null : 100*(fraction-(i===4 ? 1 : 0));
  }
  if(i===7){
    const [cash,capex]=cashPair(d);
    return cash && capex ? (cash.value-capex.value)/1000 : null;
  }
  const entry=preferredInput(d,i===5?'cash':'capex');
  return entry ? entry.value/1000 : null;
}
function preferredIncome(d){return d.financials?.release.operating ?? d.financials?.report.operating;}
function incomeStatement(d){
  return [d.financials?.release,d.financials?.report].find(s=>s?.operating && s.revenue);
}
function growthStatement(d){
  return [d.financials?.release,d.financials?.report].find(s=>s?.revenue && s.priorRevenue);
}
function metricUnit(i){
  if(i===1)return unit('USD/share');
  if(i===3||i===4)return '%';
  return unit('USD billions');
}
function metricNumber(value,i){
  if(value==null)return '—';
  if(i===3||i===4)return (i===4 && value>0?'+':'')+numbers(value,1)+'%';
  return i===1?numbers(value,2):value.toLocaleString('en-US',{maximumFractionDigits:3});
}
function renderMetricChart(){
  const names=metricNames.map(pair=>tr(...pair)),values=records.map(d=>financialValue(d,metric));
  const unitLabel=metricUnit(metric);
  const lo=Math.min(0,...values.filter(Number.isFinite)),hi=Math.max(0,...values.filter(Number.isFinite));
  const interval=(hi-lo||1)/3, mag=10**Math.floor(Math.log10(interval));
  const step=Math.ceil(interval/mag)*mag, bottom=Math.floor(lo/step)*step;
  const top=Math.max(bottom+step,Math.ceil(hi/step)*step);
  const pos=v=>100*(v-bottom)/(top-bottom),base=pos(0);
  $('chartTitle').textContent=tr('Quarterly performance','四半期推移');
  $('axisUnit').textContent=unitLabel;
  const controls=$('plotTools'),label=node('label',tr('Metric','指標'));
  const picker=node('select');picker.id='chartMetric';
  names.forEach((name,i)=>picker.append(new Option(name,i)));picker.value=String(metric);
  picker.onchange=()=>{metric=Number(picker.value);plot();ledger();evidence();$('chartMetric').focus({preventScroll:true});};
  label.append(picker);controls.replaceChildren(label);
  const scale=$('scale'),bars=$('bars'),periods=$('periods');
  scale.replaceChildren();bars.replaceChildren();periods.replaceChildren();
  bars.style.background='none';
  bars.style.setProperty('--metric-base',base+'%');
  for(let i=0;i<=Math.round((top-bottom)/step);i++){
    const value=bottom+i*step, mark=node('span',Number(value.toPrecision(6)));
    mark.style.bottom=pos(value)+'%';scale.append(mark);
    const grid=node('span',null,value===0?'zero-line':'grid-line');grid.style.bottom=pos(value)+'%';bars.append(grid);
  }
  bars.setAttribute('aria-label',names[metric]+' · '+unitLabel);
  for(const n of [bars,periods])n.style.gridTemplateColumns='repeat('+records.length+',minmax(0,1fr))';
  records.forEach((d,i)=>{
    const v=values[i],t=periodText(d.period)+' · '+names[metric]+' · '+metricNumber(v,metric)+(metric===3||metric===4?'':' '+unitLabel);
    const btn=node('button',null,'plot-col');btn.type='button';btn.dataset.i=i;btn.title=t;btn.setAttribute('aria-label',t);btn.setAttribute('aria-pressed',String(i===selected));
    if(v!=null){
      const bar=node('i');bar.style.position='absolute';bar.style.bottom=Math.min(pos(v),base)+'%';
      bar.style.height=Math.abs(pos(v)-base)+'%';btn.append(bar);
    }else btn.append(node('span','—','missing-bar'));
    bars.append(btn);
    const p=node('span',d.period.slice(-2));p.append(node('small','FY'+d.period.slice(4,6)));periods.append(p);
  });
  $('plotValue').textContent=periodText(records[selected].period)+' · '+metricNumber(values[selected],metric)+(metric===3||metric===4?'':' '+unitLabel);
}
function metricCell(d,i,sub){
  const cell=node('td',metricNumber(financialValue(d,i),i),'number-cell');
  if(sub!=null)cell.append(node('small',metricNumber(financialValue(d,sub),sub)));
  return cell;
}
function renderFinancialLedger(){
  const cash=metric>=5;
  const switcher=$('ledgerViews'),makeButton=(isCash,name)=>{
    const button=node('button',name);button.type='button';button.dataset.view=isCash?'cash':'income';
    button.setAttribute('aria-pressed',String(cash===isCash));
    button.onclick=()=>{metric=isCash?5:0;plot();ledger();evidence();switcher.querySelector('[aria-pressed=true]').focus({preventScroll:true});};
    return button;
  };
  switcher.replaceChildren(makeButton(false,tr('Earnings','損益')),makeButton(true,tr('Cash flow','キャッシュフロー')));
  const fields=cash
    ? [[5],[6],[7]] : [[0,4],[2,3],[1]];
  const head=node('tr');head.append(node('th',tr('Fiscal quarter','会計四半期')));
  fields.forEach(([i,j])=>{
    const th=node('th',tr(...metricNames[i]));
    if(j!=null)th.append(node('small',j===4?tr('YoY change','前年同期比'):tr(...metricNames[j])));
    head.append(th);
  });
  if(!cash)head.append(node('th',tr('Revenue check','売上高の照合')));
  for(const th of head.children)th.scope='col';
  $('head').replaceChildren(head);
  $('ledgerUnits').textContent=cash?tr('USD billions. Capital expenditures are shown as spending.','単位は十億ドル。設備投資額は支出額で表示します。'):tr('Amounts in USD billions. Diluted EPS in USD per share.','金額は十億ドル、希薄化後EPSは1株当たりドル。');
  const rows=records.map((d,i)=>{
    const row=node('tr');row.dataset.i=i;if(selected===i)row.classList.add('active');
    const period=node('td'),button=node('button',periodText(d.period),'pick');button.type='button';
    button.dataset.i=i;button.setAttribute('aria-controls','focus');button.setAttribute('aria-pressed',String(i===selected));period.append(button);
    row.append(period,...fields.map(([m,n])=>metricCell(d,m,n)));
    if(!cash){const status=node('td');status.innerHTML=pill(d.facts[0].status);row.append(status);}
    return row;
  });
  $('rows').replaceChildren(...rows.reverse());
}
function showSources(parent,item,caption){
  if(!item)return;
  const group=node('section',null,'source-input'),title=node('h3',caption);
  group.append(title,node('p',numbers(item.value,0)+' '+unit('USD millions'),'source-amount'));
  if(item.method==='difference')
    group.append(node('p',tr('Quarter from year-to-date totals','累計値から算出')+' · '+item.sources.map(s=>numbers(s.value,0)).join(' − ')+' = '+numbers(item.value,0),'reference'));
  for(const s of item.sources){
    const url=new URL(s.url);
    if(s.anchor)url.hash=s.anchor;
    const link=node('a',(s.form||'SEC')+' · '+s.column);link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';
    group.append(link,node('p',s.row,'source-row'));
  }
  parent.append(group);
}
function renderAddedEvidence(d,reading){
  const i=metric, e=d.financials;
  const val=financialValue(d,i);
  const box=node('div',null,'fact');box.append(node('span',tr(...metricNames[i])),node('strong',metricNumber(val,i)));
  if(i!==3 && i!==4)box.append(node('small',' '+metricUnit(i)));
  reading.append(box);
  if(i===3||i===4){
    const margin=i===3,s=margin?incomeStatement(d):growthStatement(d);
    reading.append(node('p',margin?tr('Calculated as operating income ÷ revenue × 100.','営業利益 ÷ 売上高 × 100 で算出。'):tr('Calculated as (quarterly revenue ÷ revenue in the same fiscal quarter last year − 1) × 100.','（当期の売上高 ÷ 前年同期の売上高 − 1）× 100 で算出。'),'rule'));
    if(s){
      showSources(reading,margin?s.operating:s.revenue,margin?tr(...metricNames[2]):tr('Current quarter revenue','当期の売上高'));
      showSources(reading,margin?s.revenue:s.priorRevenue,margin?tr(...metricNames[0]):tr('Prior-year quarter revenue','前年同期の売上高'));
    }
  }else if(i===7){
    reading.append(node('p',tr('Calculated by this monitor as operating cash flow less cash spent on capital expenditures. Both inputs use the same filing basis.','当サイトが営業キャッシュフローから設備投資額を差し引いて算出します。両方の金額を同じ種類の開示資料から取得しています。'),'rule'));
    const [op,capex]=cashPair(d);showSources(reading,op,tr(...metricNames[5]));showSources(reading,capex,tr(...metricNames[6]));
  }else{
    const key=i===5?'cash':'capex',src=preferredInput(d,key);
    showSources(reading,src,src===e?.report[key]?tr('Periodic report','定期報告書'):tr('Earnings release','決算発表資料'));
    if(e?.release[key] && e.report[key]){
      const other=e.report[key], differs=src.value!==other.value;
      const label=other.method==='difference'?tr('Quarter from periodic report totals','定期報告書の累計差額'):tr('Quarter in the periodic report','定期報告書の四半期値');
      const details=node('details',null,'other-source');details.append(node('summary',label));details.open=differs;
      showSources(details,other,label);reading.append(details);
      if(differs)reading.append(node('p',tr('Difference (release less periodic report)','差額（決算発表資料 − 定期報告書）')+' · '+numbers(src.value-other.value,0)+' '+unit('USD millions'),'value-note'));
    }
  }
  if((i===6||i===7) && capitalScope[ticker])
    reading.append(node('p',tr(...capitalScope[ticker]),'reference'));
  if((i===6||i===7) && e?.release.capex?.sources[0].scope==='includesEnergySystems')
    reading.append(node('p',tr('Tesla includes purchases of energy generation and storage systems in capital expenditures for this period.','Teslaはこの期の設備投資額にエネルギー発電・貯蔵システムの購入を含めています。'),'reference'));
  if(val==null)reading.append(node('p',tr('The inputs for this quarter could not be established from the available filings.','手元の開示資料から、この四半期の算出に必要な値を特定できませんでした。'),'reference'));
}
function exportEvidence(item){
  if(!item)return '';
  return item.sources.map(s=>s.row+' | '+s.column+' | '+s.value+' USD million | '+s.url+(s.anchor?'#'+s.anchor:'')).join(' ; ');
}
function extraExportData(d,i){
  const e=d.financials;
  if(!e)return [];
  if(i===2)return [e.release.operating,e.report.operating].filter(Boolean);
  if(i===3){const s=incomeStatement(d);return s?[s.operating,s.revenue]:[];}
  if(i===4){const s=growthStatement(d);return s?[s.revenue,s.priorRevenue]:[];}
  if(i===7)return cashPair(d).filter(Boolean);
  return [preferredInput(d,i===5?'cash':'capex')].filter(Boolean);
}
function appendMetricCSV(header,rows,quarters,statusFilter){
  const copyRow=(r)=>Object.fromEntries(header.map((k,i)=>[k,r[i]]));
  const groups=new Map();
  for(const row of rows){
    const r=copyRow(row),key=(r.Ticker||ticker)+'/'+r['Fiscal quarter'];if(!groups.has(key))groups.set(key,r);
    const d=quarters.get(key);
    row.push(...(r.Metric==='Operating income'&&d?['', '', '', extraExportData(d,2).map(exportEvidence).join(' / ')]:['','','','']));
  }
  if(!statusFilter)for(const [key,base] of groups){
    const d=quarters.get(key);if(!d?.financials)continue;
    for(let i=3;i<metricNames.length;i++){
      const raw=financialValue(d,i),pct=i===3||i===4;
      const data=extraExportData(d,i),method=i===3?'Operating income / revenue × 100':i===4?'(Revenue / prior-year quarter revenue − 1) × 100':i===7?'Operating cash flow − capital expenditures':data[0]?.method||'';
      const r={...base,Metric:metricNames[i][0],Unit:pct?'%':'USD millions'};
      for(const col of ['Reported value','Comparison value','Comparison unit','Comparison row','Comparison column','Comparison source','Check','Automated check','Exception'])r[col]='';
      rows.push([...header.map(name=>r[name]),raw==null?'':Number((pct?raw:raw*1000).toFixed(8)),r.Unit,method,data.map(exportEvidence).join(' / ')]);
    }
  }
  header.push('Indicator value','Indicator unit','Indicator method','Indicator source inputs (USD millions)');
}
