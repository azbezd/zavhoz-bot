(() => {
  const q=document.querySelector('#q'), category=document.querySelector('#category');
  const normalize=s=>s.normalize('NFKC').toLocaleLowerCase('ru').replaceAll('ё','е').replace(/[-–—]/g,' ');
  if(!q){const back=document.querySelector('#back');try{if(back)back.href='../index.html'+(sessionStorage.getItem('catalog-query')||'');}catch{}return;}
  const params=new URLSearchParams(location.search);
  q.value=params.get('q')||'';category.value=params.get('category')||'';
  const cards=[...document.querySelectorAll('.card')];
  const search=new Map(cards.map(c=>[c,normalize(c.dataset.search)]));
  function filter(){
    const terms=normalize(q.value.trim()).split(/\s+/).filter(Boolean);let shown=0;
    document.querySelectorAll('[data-cat]').forEach(section=>{
      let count=0;
      section.querySelectorAll('.card').forEach(card=>{const match=(!category.value||category.value===section.dataset.cat)&&terms.every(t=>search.get(card).includes(t));card.hidden=!match;if(match)count++;});
      section.hidden=count===0;section.querySelector('.n').textContent=count;shown+=count;
    });
    document.querySelector('#results').textContent=`Найдено: ${shown} из ${cards.length}`;
    document.querySelector('#nores').hidden=shown>0;
    const p=new URLSearchParams();if(q.value.trim())p.set('q',q.value.trim());if(category.value)p.set('category',category.value);
    const query=p.size?'?'+p.toString():'';
    history.replaceState(null,'',location.pathname+query);
    try{sessionStorage.setItem('catalog-query',query);}catch{}
  }
  q.addEventListener('input',filter);category.addEventListener('change',filter);
  document.querySelector('#clear').addEventListener('click',()=>{q.value='';category.value='';filter();q.focus();});filter();
})();
