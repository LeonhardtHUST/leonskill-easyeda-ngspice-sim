// 探测：在普通原理图页激活时调用 getSimulationNetlistFile 会怎样？
// 返回三态：ok（拿到网表文本）/ timeout（12s 未返回，可能弹阻塞对话框）/ error
// 注意：在空白页调用可能弹模态框导致 EDA 卡住——仅在用户明确接受风险时调用。
const race = (p, ms) => Promise.race([p.then(v => v), new Promise(r => setTimeout(() => r('__T__'), ms))]);
const t0 = Date.now();
try {
  const f = await race(eda.sch_ManufactureData.getSimulationNetlistFile('probe', 'Ngspice'), 12000);
  if (f === '__T__') return JSON.stringify({ status: 'timeout', ms: Date.now() - t0 });
  if (!f) return JSON.stringify({ status: 'error', detail: 'no file', ms: Date.now() - t0 });
  const text = await f.text();
  return JSON.stringify({ status: 'ok', ms: Date.now() - t0, len: text.length, head: text.slice(0, 600) });
} catch (e) {
  return JSON.stringify({ status: 'error', detail: String(e).slice(0, 200), ms: Date.now() - t0 });
}
