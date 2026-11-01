#!/bin/bash
# ⚠ 实验特性：在 EDA 的仿真页上按 plan 重建仿真线路图（放置器件/布线/设值）。
#   会清空目标仿真页现有内容（含手工探针）——执行前请确认已备份工程文件。
#
# 用法: build_sim_page.sh <工程名> <ir_nets.json> <simlib_pack.json> [--yes]
# 前置: bridge 运行中；工程已打开；仿真页（doctype=8）已人工打开。
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/bin:$PATH"
PY="/c/Users/Leon/.workbuddy/binaries/python/versions/3.13.12/python.exe"
NODE="/c/Users/Leon/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"

PROJECT="$1"; NETS="$2"; PACK="$3"; CONFIRM="$4"
if [ -z "$PROJECT" ] || [ -z "$NETS" ] || [ -z "$PACK" ]; then
  sed -n '2,8p' "$0" >&2; exit 2
fi
if [ "$CONFIRM" != "--yes" ]; then
  echo "⚠ 将清空 [$PROJECT] 仿真页并进行重建。确认请加 --yes 参数重跑。" >&2
  exit 1
fi

W="$TMP/simpage_$$"; mkdir -p "$W"
"$PY" "$SCRIPT_DIR/router.py" "$NETS" "$W/plan.json" >/dev/null
echo "plan ready" >&2

"$PY" - "$W/plan.json" "$PACK" "$W/plan_exec.json" <<'PYEOF'
import json, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
pack = json.load(open(sys.argv[2], encoding="utf-8"))
mapping = {}
kind_key = {"R": "R", "C": "C", "L": "R", "QNPN": "Q", "QPNP": "Q", "VSRC": "VSIN"}
for k, v in pack["parts"].items():
    mapping[k] = {"uuid": v["deviceUuid"], "libraryUuid": "0819f05c4eef4c71ace90d822a990e87"}
out = {"plan": plan, "simlib": mapping,
       "kindKey": kind_key,
       "flags": {"Power": pack["parts"]["FLAG_POWER"]["deviceUuid"],
                 "Ground": pack["parts"]["FLAG_GND"]["deviceUuid"]}}
json.dump(out, open(sys.argv[3], "w", encoding="utf-8"), ensure_ascii=False)
print("exec json ready")
PYEOF

cat > "$W/build.js" <<'EOF'
const P = __PLAN_JSON__;
const LIB = P.simlib, KEY = P.kindKey, FLAGS = P.flags, PLAN = P.plan;
const race = (p, ms) => Promise.race([p.then(v => v), new Promise(r => setTimeout(() => r('__HANG__'), ms))]);
const out = { placed: [], failed: [], wires: 0, flags: 0 };

const tree = await eda.dmt_EditorControl.getSplitScreenTree();
const simTab = (tree?.tabs || []).find(t => t.data?.doctype === 8);
if (!simTab) return JSON.stringify({ error: '__NO_SIM_TAB__' });
await eda.dmt_EditorControl.activateDocument(simTab.tabId);
await new Promise(r => setTimeout(r, 1800));

const comps = await eda.sch_PrimitiveComponent.getAll();
const junk = comps.filter(c => c.getState_ComponentType() !== 'sheet').map(c => c.getState_PrimitiveId());
if (junk.length) { try { await eda.sch_PrimitiveComponent.delete(junk); } catch (e) {} }

const placedById = {};
for (const c of PLAN.components) {
  const lib = LIB[KEY[c.simKind]];
  if (!lib) { out.failed.push([c.designator, 'no sim device']); continue; }
  const comp = await race(eda.sch_PrimitiveComponent.create(
    { libraryUuid: lib.libraryUuid, uuid: lib.uuid }, c.x, c.y, '', c.rotation, false, false, false), 6000);
  if (!comp || comp === '__HANG__') { out.failed.push([c.designator, 'create hang']); continue; }
  const id = comp.getState_PrimitiveId();
  placedById[c.designator] = id;
  try {
    const attrs = await eda.sch_PrimitiveAttribute.getAll(id);
    for (const a of attrs) {
      const k = a.getState_Key();
      let v = null;
      if (k === 'Designator') v = c.designator;
      else if (k === 'Value' && c.value) v = String(c.value).replace(/Ω/g, '');
      else if (k === 'Voltage[V]' && c.value) v = c.value;
      else if (k === 'Frequency[Hz]') v = '1k';
      if (v !== null) { const aa = a.toAsync(); aa.setState_Value(v); await aa.done(); }
    }
  } catch (e) {}
  out.placed.push(c.designator);
}
for (const sup of (PLAN.supply_components || [])) {
  const lib = LIB.VDC;
  if (!lib) continue;
  const comp = await race(eda.sch_PrimitiveComponent.create(
    { libraryUuid: lib.libraryUuid, uuid: lib.uuid }, sup.x, sup.y, '', 0, false, false, false), 6000);
  if (!comp || comp === '__HANG__') continue;
  try {
    const attrs = await eda.sch_PrimitiveAttribute.getAll(comp.getState_PrimitiveId());
    for (const a of attrs) {
      const k = a.getState_Key();
      if (k === 'Designator') { const aa = a.toAsync(); aa.setState_Value('VP' + Math.abs(Math.round(sup.voltage))); await aa.done(); }
      else if (k === 'Voltage[V]') { const aa = a.toAsync(); aa.setState_Value(sup.voltage + 'V'); await aa.done(); }
    }
  } catch (e) {}
}
for (const f of (PLAN.netflags || [])) {
  try {
    await race(eda.sch_PrimitiveComponent.createNetFlag(
      f.flagType === 'Power' ? 'Power' : 'Ground', f.net, f.x, f.y, 0, false), 5000);
    out.flags++;
  } catch (e) {}
}
for (const w of (PLAN.wires || [])) {
  for (const s of w.segments) {
    if (Math.abs(s[0] - s[2]) < 1e-6 && Math.abs(s[1] - s[3]) < 1e-6) continue;
    try {
      await eda.sch_PrimitiveWire.create([s[0], s[1], s[2], s[3]], w.net || undefined, null, null, null);
      out.wires++;
    } catch (e) { /* 个别导线失败不阻塞 */ }
  }
}
return JSON.stringify(out);
EOF

"$NODE" -e "
const fs = require('fs');
const tpl = fs.readFileSync(process.argv[1], 'utf8');
const plan = fs.readFileSync(process.argv[2], 'utf8');
fs.writeFileSync(process.argv[3], tpl.replace('__PLAN_JSON__', plan));
" "$W/build.js" "$W/plan_exec.json" "$W/build_final.js"

echo "执行重建（会清空仿真页）..." >&2
bash "$SCRIPT_DIR/eda_exec.sh" "$PROJECT" "$W/build_final.js" 28 2>/dev/null | "$NODE" -e "
let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{
  try{const r=JSON.parse(d);console.log('重建结果:', r.result || r.error)}catch(e){console.log('响应异常:', d.slice(0,300))}
})"
