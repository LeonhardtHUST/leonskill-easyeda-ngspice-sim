const PAGE = '__PAGE_UUID__';
const SEG = '__SEG__';

const tree = await eda.dmt_EditorControl.getSplitScreenTree();
const tabs = tree?.tabs || [];
const pageTab = tabs.find(t => String(t.tabId).startsWith(PAGE));
if (pageTab) { await eda.dmt_EditorControl.activateDocument(pageTab.tabId); }
else { await eda.dmt_EditorControl.openDocument(PAGE); }
await new Promise(r => setTimeout(r, 1500));

const doc = await eda.dmt_SelectControl.getCurrentDocumentInfo();
const docOk = doc && doc.uuid === PAGE;

if (SEG === 'comps') {
  const comps = await eda.sch_PrimitiveComponent.getAll();
  const out = [];
  for (const c of comps) {
    const id = c.getState_PrimitiveId();
    const rec = {
      id,
      ct: c.getState_ComponentType(),
      des: c.getState_Designator() || null,
      name: c.getState_Name() || null,
      x: c.getState_X(), y: c.getState_Y(),
      rot: c.getState_Rotation(), mir: c.getState_Mirror(),
      attrPairs: []
    };
    try { const comp = c.getState_Component(); rec.lu = comp?.libraryUuid; rec.cu = comp?.uuid; } catch (e) {}
    try {
      const attrs = await eda.sch_PrimitiveAttribute.getAll(id);
      rec.attrPairs = attrs.map(a => {
        let v = null;
        try { v = a.getState_Value(); } catch (e) {}
        return { k: a.getState_Key(), v };
      });
    } catch (e) {}
    out.push(rec);
  }
  return JSON.stringify({ seg: 'comps', docOk, n: out.length, comps: out });
}

if (SEG === 'pins') {
  const comps = await eda.sch_PrimitiveComponent.getAll();
  const out = {};
  for (const c of comps) {
    if (c.getState_ComponentType() !== 'part') continue;
    const id = c.getState_PrimitiveId();
    try {
      const pins = await eda.sch_PrimitiveComponent.getAllPinsByPrimitiveId(id);
      out[id] = (pins || []).map(p => {
        const r = {};
        try { r.n = p.getState_Name(); } catch (e) {}
        try { r.num = p.getState_PinNumber(); } catch (e) {}
        try { r.x = p.getState_X(); r.y = p.getState_Y(); } catch (e) {}
        return r;
      });
    } catch (e) { out[id] = []; }
  }
  return JSON.stringify({ seg: 'pins', docOk, pins: out });
}

if (SEG === 'wires') {
  const wires = await eda.sch_PrimitiveWire.getAll();
  const out = wires.map(w => ({
    id: w.getState_PrimitiveId(),
    line: w.getState_Line(),
    net: w.getState_Net() || ''
  }));
  return JSON.stringify({ seg: 'wires', docOk, n: out.length, wires: out });
}

return JSON.stringify({ err: 'unknown segment ' + SEG });
