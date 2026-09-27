// 期①前端调用的 HTTP 级复刻：按 Step1/Step2 实际发出的请求序列跑一遍大表，
// 把界面会显示的每个数字打出来，供与 pandas / verify_http 对拍。浏览器工具被权限拦时用它兜底。
const BASE = 'http://127.0.0.1:8000';

async function req(path, init) {
  const t0 = Date.now();
  const res = await fetch(BASE + path, init);
  const text = await res.text();
  const ms = Date.now() - t0;
  let body;
  try { body = JSON.parse(text); } catch (e) { body = { raw: text.slice(0, 120) }; }
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status} ${text.slice(0, 200)}`);
  return { body, ms, bytes: Buffer.byteLength(text) };
}
const jpost = (body) => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const MiB = (n) => (n / 1024 / 1024).toFixed(2) + ' MiB';

function show(label, v) { console.log(`  ${label}: ${v}`); }

(async () => {
  const health = (await req('/api/health')).body;
  show('后端', `v${health.version} · 工作区上限 ${health.limits.workspaces} · 单元格上限 ${health.limits.cellsPerWorkspace.toLocaleString()}`);

  const opened = await req('/api/ws/dataset', jpost({ filename: 'big40.csv' }));
  const { meta, page } = opened.body;
  const wsId = meta.wsId;
  show('建区', `${opened.ms} ms · 响应 ${MiB(opened.bytes)} · wsId=${wsId}`);
  show('meta', `${meta.rowCount} 行 × ${meta.colCount} 列 · 单元格 ${meta.cellCount.toLocaleString()} · ${meta.freqLabel} · ${meta.timeFormat}`);
  show('分页', `offset=${page.offset} limit=${page.limit} → 本页 ${page.rows.length} 行 / 共 ${page.total} 行 · 整个建区响应 ${MiB(opened.bytes)}`);
  show('首页首行', JSON.stringify(page.rows[0]).slice(0, 90));

  // Step2 数字条
  const ov = await req(`/api/ws/${wsId}/overview`);
  const o = ov.body;
  show('overview', `${ov.ms} ms · 缺失 ${o.missingCells}/${o.missingDenominator} = ${o.missingRate.toFixed(4)}% · 重复 ${o.duplicateRows}/${o.rowCount} = ${o.duplicateRate.toFixed(4)}% · 内存 ${MiB(o.memoryBytes)}`);

  // 翻到第 4 页（Step2 分页按钮）
  const p4 = await req(`/api/ws/${wsId}/rows?offset=150&limit=50`);
  show('第 4 页', `第 ${p4.body.page.offset + 1}–${p4.body.page.offset + p4.body.page.rows.length} 行 · ${p4.ms} ms · ${MiB(p4.bytes)}`);
  const firstId = (r) => JSON.stringify(r).slice(0, 60);
  show('对照第 1 页第 6 行', firstId(page.rows[5]));
  show('后端重取第 6 行', firstId((await req(`/api/ws/${wsId}/rows?offset=5&limit=1`)).body.page.rows[0]));

  // 重采样预演 + 执行（11000 行 30min → 60min）
  const prev = await req(`/api/ws/${wsId}/resample-preview?targetMinutes=60`);
  show('预演', `${prev.ms} ms · ${prev.body.currentRows} → ${prev.body.projectedRows} 行 · 有值桶 ${prev.body.filledBuckets} · 空桶 ${prev.body.emptyBuckets} · 压缩比 ${prev.body.compression}`);
  const rs = await req(`/api/ws/${wsId}/op/resample`, jpost({ targetMinutes: 60, method: 'mean' }));
  show('重采样', `${rs.ms} ms · v${rs.body.version} · ${rs.body.summary}`);
  show('一致性', `后端 ${rs.body.meta.rowCount} 行 vs 预演 ${prev.body.projectedRows} 行 → ${rs.body.meta.rowCount === prev.body.projectedRows ? '相等' : '不相等'}`);

  // 撤销回到上一版本
  const back = await req(`/api/ws/${wsId}/restore?limit=50`, jpost({ version: rs.body.version - 1 }));
  show('撤销', `v${back.body.meta.version} · ${back.body.meta.rowCount} 行 · 本页 ${back.body.page.rows.length} 行`);

  // 整列取数（过渡期通道：第③~⑤步的浏览器算法用）
  const cols = await req(`/api/ws/${wsId}/columns?keys=${encodeURIComponent('传感器01')}&max_rows=0`);
  const vals = cols.body.columns['传感器01'];
  const nums = vals.filter(v => v !== null).map(Number);
  const mean = nums.reduce((s, v) => s + v, 0) / nums.length;
  show('整列传感器01', `${cols.ms} ms · ${MiB(cols.bytes)} · ${vals.length} 值 · 缺失 ${vals.length - nums.length} · 均值 ${mean.toFixed(4)}`);

  await req(`/api/ws/${wsId}`, { method: 'DELETE' });
  console.log('  已关闭工作区');
})().catch(e => { console.error('FAILED: ' + e.message); process.exit(1); });
