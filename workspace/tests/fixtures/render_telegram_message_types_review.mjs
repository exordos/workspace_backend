/** Offline review of the complete Zulip corpus through the unchanged Telegram converter. */
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import * as url from 'node:url';
import * as module from 'node:module';
import * as assert from 'node:assert/strict';

const directory = path.dirname(url.fileURLToPath(import.meta.url));
const repository = path.resolve(directory, '../../..');
const workspace = process.env.WORKSPACE_UI_SOURCE_DIR ?? path.resolve(repository, '../workspace_ui');
const requireUi = module.createRequire(path.join(workspace, 'package.json'));
const { JSDOM, VirtualConsole } = requireUi('jsdom');
const source = new JSDOM(await fs.readFile(path.join(directory, 'zulip_messages_preview.html'), 'utf8'), { virtualConsole: new VirtualConsole() });
const sourceDocument = source.window.document;
const data = JSON.parse(await fs.readFile(path.join(directory, 'telegram_message_types_review.json'), 'utf8'));
const escape = value => String(value).replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
const categories = {quotes:'Цитаты',formatting:'Текст и форматирование',mentions:'Упоминания',media:'Файлы и медиа',links:'Ссылки',special:'Специальные типы',runtime:'Чаты и события',native:'Нативный Telegram'};

function telegramBody(value) {
  const text = value.text;
  const entities = [...value.entities].sort((a,b) => a.offset-b.offset || b.length-a.length);
  function render(start,end,values) {
    let output = '', cursor = start;
    while (values.length) {
      const [entity,...rest] = values;
      const right = entity.offset+entity.length;
      if (entity.offset < cursor || right > end) { values=rest;continue; }
      output += escape(text.slice(cursor,entity.offset));
      let body = render(entity.offset,right,rest.filter(item => item.offset < right));
      const tags={MessageEntityBold:'strong',MessageEntityItalic:'em',MessageEntityStrike:'del',MessageEntityUnderline:'u',MessageEntityCode:'code',MessageEntityPre:'pre',MessageEntityBlockquote:'blockquote'};
      if (tags[entity.type]) body=`<${tags[entity.type]}>${body}</${tags[entity.type]}>`;
      if (entity.type==='MessageEntitySpoiler') body=`<span class="telegram-spoiler" tabindex="0" title="Спойлер: фокус или наведение раскрывает текст">${body}</span>`;
      if (entity.type==='MessageEntityTextUrl') body=`<a href="${escape(/^(https?:|mailto:|tg:)/.test(entity.url) ? entity.url : '#')}">${body}</a>`;
      output += body;cursor=right;values=rest.filter(item=>item.offset>=right);
    }
    return output+escape(text.slice(cursor,end));
  }
  return `<div class="telegram-text">${render(0,text.length,entities)}</div>`;
}

function semanticText(node, depth=0) {
  if (node.nodeType===3) return node.textContent;
  if (node.nodeType!==1) return '';
  if (node.matches('[data-workspace-quote]')) {
    const [header,body]=node.children;
    const inside=semanticText(body).trim().split('\n').map(line=>'│ '+line).join('\n');
    return `\n┌ ${header.textContent.trim()}\n${inside}\n└\n`;
  }
  if (node.classList.contains('fixture-time')) return '';
  const content=[...node.childNodes].map(child=>semanticText(child,depth)).join('');
  if (node.tagName==='BR') return '\n';
  if (node.tagName==='LI') return '• '+content+'\n';
  if (['P','DIV','PRE','H1','H2','H3','UL','OL'].includes(node.tagName)) return content+'\n';
  return content;
}

const priority=['nested_message_quote','triple_nested_quote','multiple_commented_quotes','reply_to_multiple_commented_quotes','partial_multiple_commented_quotes','partial_nested_direct_context_quote','nested_direct_context_quote'];
const rows=[...data.zulip_cases].sort((a,b)=>{
 const ai=priority.indexOf(a.example.name),bi=priority.indexOf(b.example.name);
 return (ai<0?999:ai)-(bi<0?999:bi);
});
assert.equal(rows.length,79);
assert.equal(new Set(rows.map(row=>row.example.name)).size,79);
const originalNames=[...sourceDocument.querySelectorAll('article.example')].map(node=>node.id);
assert.deepEqual(new Set(originalNames),new Set(rows.map(row=>row.example.name)));

const cards=rows.map((row,index)=>{
 const example=row.example;
 const original=sourceDocument.getElementById(example.name);
 assert.ok(original,example.name);
 const left=original.querySelector('.column.zulip').cloneNode(true);
 const middle=original.querySelector('.column.workspace').cloneNode(true);
 left.querySelector('h3').innerHTML='Zulip <small>Исходный пример</small>';
 middle.querySelector('h3').innerHTML='Workspace <small>Эталон содержимого перед отправкой</small>';
 const notes=row.notes.map(note=>`<p>${escape(note)}</p>`).join('');
 const proposed=row.category==='quotes'?semanticText(middle.querySelector('.workspace-native')).replace(/\n{3,}/g,'\n\n').trim():null;
 const references=Object.keys(row.telegram.references).length?`<div class="reference-note">Служебные references переданы в отправку; их доставка и разрешение здесь не выполняются.</div>`:'';
 const issue=row.notes.length>0;
 return `<article class="example" id="${escape(example.name)}" data-category="${row.category}" data-review="${issue}" data-search="${escape((example.name+' '+example.description+' '+categories[row.category]+' '+row.telegram.text).toLowerCase())}">
 <header class="example-heading"><div><span class="case-number">${String(index+1).padStart(2,'0')}</span><a href="#${escape(example.name)}">${escape(example.name.replaceAll('_',' '))}</a></div><span class="badge ${issue?'attention':'supported'}">${escape(categories[row.category])} · ${issue?'нужно проверить':'снимок конвертера'}</span></header>
 <p class="description">${escape(example.description)}</p>
 <div class="comparison">${left.outerHTML}${middle.outerHTML}<section class="column telegram"><h3>Telegram <small>Текущий результат, без исправлений</small></h3><div class="telegram-bubble">${telegramBody(row.telegram)}</div>${references}<details><summary>Текст, UTF-16 entities и references</summary><pre>${escape(JSON.stringify(row.telegram,null,2))}</pre></details><details><summary>Обратная конвертация в Workspace — точный Markdown</summary><pre>${escape(row.telegram.roundtrip)}</pre></details></section></div>
 ${notes?`<div class="observed"><strong>Наблюдаемое сейчас</strong>${notes}</div>`:''}
 <div class="proposal"><strong>Предложение для ревью — ещё не реализовано</strong><p>${escape(row.proposal)}</p>${proposed?`<details class="proposal-text"><summary>Схема сохранения всех цитат и комментариев</summary><pre>${escape(proposed)}</pre></details>`:''}</div>
 <details class="fixture-data"><summary>Полный исходный пример Zulip и данные конвертации</summary><pre>${escape(JSON.stringify(row,null,2))}</pre></details></article>`;
}).join('\n');

const native=data.native_telegram_cases.map((row,index)=>{
 const e=row.example;
 return `<article class="example native-case" id="native-${escape(e.name)}" data-category="native" data-review="false" data-search="${escape(('нативный telegram '+e.name+' '+e.description).toLowerCase())}"><header class="example-heading"><div><span class="case-number">TG-${String(index+1).padStart(2,'0')}</span><a href="#native-${escape(e.name)}">${escape(e.name.replaceAll('_',' '))}</a></div><span class="badge">${e.direction==='outgoing'?'Workspace → Telegram':'Telegram → Workspace'}</span></header><p class="description">${escape(e.description)}</p><div class="native-grid"><section><h3>Telegram: текст и entities</h3><div class="telegram-bubble">${telegramBody(row.telegram)}</div></section><section><h3>Workspace: текущий точный Markdown</h3><pre>${escape(row.telegram.roundtrip)}</pre></section></div><details class="fixture-data"><summary>Все исходные данные</summary><pre>${escape(JSON.stringify(row,null,2))}</pre></details></article>`;
}).join('\n');
const css=[...sourceDocument.querySelectorAll('style')].map(style=>style.textContent).join('\n');
const quoteCount=rows.filter(row=>row.category==='quotes').length;
const attentionCount=rows.filter(row=>row.notes.length).length;
const html=`<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>Telegram: типы сообщений — ревью до исправлений</title><style>${css}
.page-header,.toolbar-inner,.container{max-width:1760px}.comparison{grid-template-columns:repeat(3,minmax(0,1fr))}.telegram .telegram-bubble{margin:12px 0 20px}.telegram-text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.65;min-height:24px}.telegram-bubble{padding:14px 16px;background:#e6f3e1;border:1px solid #d4e6cf;border-radius:13px;overflow-wrap:anywhere}.telegram-text pre{white-space:pre-wrap;margin:8px 0;background:#d5e7d1;padding:10px;border-radius:7px}.telegram-text code{font-size:12px}.telegram-text blockquote{border-left:3px solid #66a765;margin:8px 0;padding:8px 12px}.telegram-spoiler{background:#526255;color:#526255;border-radius:3px;cursor:pointer}.telegram-spoiler:hover,.telegram-spoiler:focus{background:#c4ddbf;color:inherit}.status-banner{margin:18px 0 0;border-left:4px solid #d1902e;background:#fff6e7;border-radius:5px;padding:14px 18px;font-size:13px;line-height:1.65}.status-banner strong{display:block}.quick-links{display:flex;gap:9px;flex-wrap:wrap;margin-top:18px}.quick-links a{font-size:12px;border:1px solid #d4dfe9;padding:7px 11px;border-radius:7px;background:white}.observed,.proposal{padding:15px 22px;border-top:1px solid #e5e8ed;font-size:12px;line-height:1.6}.observed{background:#fff0ec;color:#793e30}.proposal{background:#f1f6fd;color:#3c526c}.observed p,.proposal p{margin:5px 0 0}.proposal pre,.native-grid pre{font:12px/1.7 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#fff;padding:15px;border-radius:7px;color:#2e4055}.proposal-text{margin-top:10px}.badge.attention{color:#9a4936;background:#fff0eb}.reference-note{font-size:11px;line-height:1.6;background:#f2f4f8;padding:9px;border-radius:5px}.native-grid{display:grid;grid-template-columns:1fr 1fr;border-top:1px solid #e4e8ed}.native-grid>section{padding:10px 22px}.category-select{border:1px solid #ccd6e2;border-radius:7px;padding:9px;background:white;max-width:240px}.legend{font-size:12px;color:#62748a;max-width:1100px;margin-top:12px}.count{font-size:11px}.source-section{font-size:21px;scroll-margin-top:100px;margin:30px 0 18px}.column h3 small{font-size:10px}.footer{line-height:1.8}.telegram-text:empty:after{content:'Текст отсутствует; проверьте references и путь отправки медиа/ответа';color:#6a796c;font-size:11px}.intro{max-width:1120px}.toolbar-inner{flex-wrap:wrap;gap:12px}.search{max-width:520px}
@media(max-width:1100px){.comparison{grid-template-columns:1fr}.column+.column{border-left:0;border-top:1px solid #e4e8ed}.workspace-native{margin-left:0}.zulip .message-body{margin-left:0}.column details{margin-left:0}.native-grid{grid-template-columns:1fr}.category-select{flex:1}}
@media print{.comparison{grid-template-columns:1fr}.proposal-text{display:block}.example{break-inside:auto}.status-banner{border:1px solid #d1902e}}
</style></head><body><header class="page-header"><div class="eyebrow">REVIEW BEFORE IMPLEMENTATION · 01.11.2026</div><h1>Telegram: все типы сообщений из каталога Zulip</h1><p class="intro">79 примеров Zulip + 17 нативных примеров Telegram. Три колонки показывают исходный Zulip, содержимое Workspace и текущий результат неизменённого Telegram-конвертера. Цитаты вынесены в начало; ниже — остальные типы.</p><div class="status-banner"><strong>Страница для согласования до исправлений</strong>Красный блок описывает наблюдаемое сейчас. Синий блок — предложение, которое ещё не реализовано и не выдано за работающий результат. Превью Telegram иллюстрирует текст и MTProto entities, а не является скриншотом или подтверждением доставки в Telegram. Все пользователи, сообщения и файлы синтетические. Страница работает офлайн.</div><p class="legend">Текстовые references, загрузки медиа, reply, выбор аккаунта, уведомления, реакции и редактирование имеют отдельные runtime-пути. На этой странице они видны в данных и плане проверки; живую доставку она не доказывает. Нативные вложенные blockquote entities не поддерживаются Telegram, поэтому для вложенных цитат предложено сохранить уровни явными текстовыми маркерами: <a href="https://core.telegram.org/bots/api#formatting-options" target="_blank" rel="noopener noreferrer">ограничения Telegram</a>.</p><nav class="quick-links"><a href="#nested_message_quote">Двойная цитата</a><a href="#triple_nested_quote">Тройная цитата</a><a href="#multiple_commented_quotes">Несколько цитат и комментарии</a><a href="#partial_multiple_commented_quotes">Частичные цитаты</a><a href="#native-cases">Нативный Telegram</a></nav></header>
<div class="toolbar"><div class="toolbar-inner"><label class="sr-only" for="search">Поиск</label><input class="search" id="search" type="search" placeholder="Поиск по типу, названию или тексту…"><label class="sr-only" for="category">Категория</label><select id="category" class="category-select"><option value="all">Все категории</option>${Object.entries(categories).map(([key,label])=>`<option value="${key}">${label}</option>`).join('')}</select><label class="filter"><input id="review-only" type="checkbox"> Замечания к текущему результату</label><output id="count" class="count" aria-live="polite">96 / 96</output></div></div>
<main class="container"><h2 class="source-section">Каталог Zulip: 79 случаев · ${quoteCount} с цитатами · ${attentionCount} с явными замечаниями</h2>${cards}<h2 class="source-section" id="native-cases">Нативный Telegram: 17 исходных примеров</h2>${native}<p id="empty" class="empty-state" hidden>Нет подходящих примеров.</p></main><footer class="footer">Offline · Из production-конвертера до исправлений · 79/79 примеров zulip_messages.json + 17/17 telegram_messages.json<br>Исходные тексты, entities, references и обратная конвертация доступны под каждым примером. Встроенные иллюстрации медиа синтетические.</footer>
<script>const cards=[...document.querySelectorAll('article.example')];function filter(){const q=document.querySelector('#search').value.toLowerCase().trim(),category=document.querySelector('#category').value,review=document.querySelector('#review-only').checked;let visible=0;for(const card of cards){card.hidden=!(card.dataset.search.includes(q)&&(category==='all'||category===card.dataset.category)&&(!review||card.dataset.review==='true'));if(!card.hidden)visible++;}document.querySelector('#count').textContent=visible+' / '+cards.length;document.querySelector('#empty').hidden=visible!==0;}document.querySelector('#search').addEventListener('input',filter);document.querySelector('#category').addEventListener('change',filter);document.querySelector('#review-only').addEventListener('change',filter);function toggle(header){const block=header.parentElement,open=block.classList.toggle('open');block.classList.toggle('is-open',open);header.setAttribute('aria-expanded',String(open));block.querySelector(':scope > .spoiler-content')?.setAttribute('aria-hidden',String(!open));}document.addEventListener('click',event=>{const h=event.target.closest('.spoiler-header');if(h)toggle(h);});document.addEventListener('keydown',event=>{if((event.key==='Enter'||event.key===' ')&&event.target.matches('.spoiler-header')){event.preventDefault();toggle(event.target);}});</script></body></html>`;
const output=path.join(repository,'docs/telegram_message_types_review.html');
const styleEnd=html.indexOf('</style>')+8;
const outputHtml=html.slice(0,styleEnd).replace(/[ \t]+$/gm,'')+html.slice(styleEnd).replace(/[ \t]+$/gm,spaces=>spaces.replaceAll(' ','&#32;').replaceAll('\t','&#9;'));
await fs.writeFile(output,outputHtml);
const rendered=new JSDOM(html,{virtualConsole:new VirtualConsole()});
assert.equal(rendered.window.document.querySelectorAll('article.example').length,96);
assert.equal(rendered.window.document.querySelectorAll('.comparison').length,79);
assert.equal(rendered.window.document.querySelectorAll('.comparison>.column').length,237);
for (const id of priority) assert.ok(rendered.window.document.getElementById(id),id);
for(const element of rendered.window.document.querySelectorAll('[src]')) assert.match(element.getAttribute('src'),/^data:/);
rendered.window.close();source.window.close();
console.log(JSON.stringify({file:output,zulip:79,nativeTelegram:17,quotes:quoteCount,attention:attentionCount,sizeBytes:Buffer.byteLength(outputHtml)}));
