// Helpers compartidos por dashboard.html y config.html
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

function el(tag, props = {}, ...children) {
  const e = Object.assign(document.createElement(tag), props);
  e.append(...children);  // textContent/append: los comercios vienen de correos, nunca innerHTML
  return e;
}

// Sugerencias de subcategoría filtradas por categoría: un solo <datalist id="subcategories">
// que se rellena al enfocar el campo con las subcategorías ya usadas en esa categoría.
function subcategoryIndex(items) {
  const index = {};
  items.forEach(x => x.category && x.subcategory
    && (index[x.category.trim().toLowerCase()] ??= new Set()).add(x.subcategory));
  return index;
}

function linkSubcategory(input, getCategory, getIndex) {
  input.setAttribute('list', 'subcategories');
  input.addEventListener('focus', () => {
    const subs = getIndex()[getCategory().trim().toLowerCase()] ?? [];
    document.getElementById('subcategories').replaceChildren(...[...subs].sort().map(v => el('option', { value: v })));
  });
}
