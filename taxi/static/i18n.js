"use strict";
// One shared catalog for browser labels and server-generated brief metadata.
const I18N = {
  language: "zh", dictionary: {}, reverse: {}, saved: null,
  read(store, key) { try { return store.getItem(key); } catch { return null; } },
  write(store, key, value) { try { store.setItem(key, value); } catch { /* Storage may be blocked. */ } },
  apply(root = document.body) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      if (node.parentElement?.closest('script,style,textarea,pre,code,[data-no-i18n]')) continue;
      node.nodeValue = t(node.nodeValue);
    }
    for (const el of root.querySelectorAll('[placeholder]')) el.placeholder = t(el.placeholder);
  },
  restore(root) {
    if (!this.saved) return;
    for (const item of this.saved.fields || []) {
      const el = item.id ? document.getElementById(item.id) : [...root.querySelectorAll('input')]
        .find(e => e.closest('fieldset')?.id === item.group && e.value === item.value);
      if (!el || !root.contains(el)) continue;
      if (el.type === 'checkbox') el.checked = item.checked;
      else if (el.tagName !== 'SELECT' || [...el.options].some(o => o.value === item.value)) el.value = item.value;
    }
  }
};
function t(value) {
  if (typeof value !== 'string') return value;
  const text = value.trim(), key = I18N.reverse[text] || text;
  const translated = I18N.language === 'en' ? I18N.dictionary[key] || text : key;
  return value.replace(text, translated);
}
function tr(key, ...values) {
  return t(key).replace(/\{(\d+)\}/g, (_, n) => String(values[Number(n)]));
}
function htmlT(markup) {
  const template = document.createElement('template'); template.innerHTML = markup;
  // Only called with application-owned markup, never user/model HTML.
  I18N.apply(template.content);
  return template.innerHTML;
}
async function initializeLanguage() {
  const stored = I18N.read(localStorage, 'taxi-language');
  I18N.language = stored === 'en' ? 'en' : 'zh';
  try {
    const response = await fetch('/static/locales/en.json');
    if (!response.ok) throw new Error('catalog');
    I18N.dictionary = await response.json();
    I18N.reverse = Object.fromEntries(Object.entries(I18N.dictionary).map(([zh,en])=>[en,zh]));
  } catch { I18N.language = 'zh'; }
  try { I18N.saved = JSON.parse(I18N.read(sessionStorage, 'taxi-language-state') || 'null'); } catch { /* Invalid saved state. */ }
  try { sessionStorage.removeItem('taxi-language-state'); } catch { /* Storage may be blocked. */ }
  document.documentElement.lang = I18N.language === 'en' ? 'en' : 'zh-CN';
  document.title = t(document.title);
  I18N.apply();
  const question = document.querySelector('#agent-question');
  question.value = t(question.value);
  const picker = document.querySelector('#language');
  picker.value = I18N.language;
  picker.onchange = () => {
    const fields = [...document.querySelectorAll('input, select, textarea')]
      .filter(el => el.id !== 'language').map(el => ({id:el.id,
        group:el.closest('fieldset')?.id, value:el.value, checked:el.checked}));
    I18N.write(sessionStorage, 'taxi-language-state', JSON.stringify({fields,
      tab:document.querySelector('#tabs button.active')?.dataset.tab,
      agentResult:typeof agentResult !== 'undefined' ? agentResult : null}));
    I18N.write(localStorage, 'taxi-language', picker.value);
    // A fresh render updates chart labels too, without repeating any model request.
    location.reload();
  };
  for (const file of ['app.js', 'agent.js']) {
    await new Promise((resolve,reject)=>{
      const script=document.createElement('script'); script.src='/static/'+file;
      script.onload=resolve; script.onerror=reject; document.body.appendChild(script);
    });
  }
}
initializeLanguage();
