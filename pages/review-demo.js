// Practice decisions live only in this document. The official register and exports use record.state.
const practice = new Map();
function renderReviewDemo(record, parent){
  const key=ticker+'/'+record.period;
  const box=node('div',null,'review-demo');
  const open=node('button',tr('Try the review controls','確認操作を試す'),'demo-trigger');
  open.type='button';
  const panel=node('div',null,'demo-panel');
  panel.id='review-practice-panel';
  open.setAttribute('aria-controls',panel.id);
  const hint=node('p',tr('Demo only. Reload to reset.','デモ表示です。再読み込みで元に戻ります。'));
  const status=node('p');
  status.setAttribute('role','status');
  const toggle=node('button',null,'download');
  toggle.type='button';
  function refresh(){
    const active=practice.has(key), state=practice.get(key);
    panel.hidden=!active;
    open.setAttribute('aria-expanded',String(active));
    open.textContent=active?tr('Close demo','デモを閉じる'):tr('Try the review controls','確認操作を試す');
    if(!active)return;
    status.replaceChildren(document.createTextNode(tr('Demo status · ','デモの確認状態 · ')),reviewBadge(state,reviewText(state)));
    toggle.textContent=state==='確認済み'?tr('Return to pending','確認待ちに戻す'):tr('Mark as reviewed','確認済みにする');
  }
  open.onclick=()=>{
    if(practice.has(key))practice.delete(key);
    else practice.set(key,record.state);
    refresh();
  };
  toggle.onclick=()=>{practice.set(key,practice.get(key)==='確認済み'?'未確認':'確認済み');refresh();};
  panel.append(hint,status,toggle);
  box.append(open,panel);parent.append(box);refresh();
}
window.addEventListener('pageshow',event=>{
  if(event.persisted && practice.size){practice.clear();draw();}
});
