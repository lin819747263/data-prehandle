// ============================================================
// 第⑤期跨语言参考实现：界面上「审计链 = 服务端命令日志的游标投影」这条规则的独立重写
//
// 这里**不 import 前端代码**，只按 src/store.js 里那三条规则各自独立实现一遍
// （logVisible / pruneUndoneLog / 由游标推计数），输入是 verify_phase5.py 从真实服务端
// 取回的命令日志与本端构造的审计记录，输出是界面上此刻会显示的那一份。
// 于是对拍比的是两套语言对同一份服务端事实的解读，而不是 Python 自己和自己比。
//
// 规则原文（与 store.js 同源）：
//   1) 条目上的 v 是这条命令在服务端落成的版本号；没有 v 的条目（导出、翻页、载入）与游标无关，恒可见。
//   2) 有 v 的条目按它自己的 wsId 找归属工作区；找不到归属（那份数据已经不在界面上）也恒可见。
//   3) 找得到归属时：v <= 该工作区当前游标 → 可见；v > 游标 → 那是「已撤销、等待重做」的一段，不显示。
//   4) 回退后又执行新命令：服务端 apply() 会把等待重做的那段日志尾巴截掉，界面这份记录必须同步截掉。
//
// 用法：node ref_phase5.mjs --input <file.json>
//   输入 { entries, workspaces, wsId, pruneTo, version, ops, meta }
// ============================================================
import fs from 'node:fs'

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name)
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : dflt
}

/** 规则 1~3：界面上此刻显示哪些条目 */
export function visibleActionLog(entries, workspaces) {
  return entries.filter(e => {
    if (!Number.isInteger(e.v)) return true
    const owner = workspaces.find(x => x.wsId && x.wsId === e.wsId)
    if (!owner) return true
    return e.v <= (owner.version ?? 0)
  })
}

/** 规则 4：执行新命令之前，把本工作区那些「等待重做」的条目就地截掉 */
export function pruneUndoneLog(entries, wsId, cursor) {
  if (!Number.isInteger(cursor)) return entries.slice()
  return entries.filter(e => e.wsId !== wsId || !Number.isInteger(e.v) || e.v <= cursor)
}

/** 顶栏那两个按钮的计数与可用性，全部由服务端游标推出来（浏览器不再压快照栈） */
export function historyCounters(meta) {
  const version = meta.version ?? 0
  const opsTotal = meta.opsTotal ?? 0
  return {
    version, opsTotal,
    undo: Math.max(0, version),
    redo: Math.max(0, opsTotal - version),
    canUndo: !!meta.canUndo,
    canRedo: !!meta.canRedo,
    undoLabel: meta.undoLabel || '',
    redoLabel: meta.redoLabel || ''
  }
}

/** 服务端视角的「当前这份数据的历史」：ops[:version]，审计链必须与它一条不差地对上 */
export function serverHistory(ops, version) {
  return ops.slice(0, Math.max(0, version)).map(o => `${o.kind}#${o.index}`)
}

/** 浏览器视角的同一件事：从审计记录里读出带 v 的那些条目的 (kind, 序号) */
function browserHistory(entries, workspaces, wsId) {
  const owner = workspaces.find(x => x.wsId === wsId)
  return visibleActionLog(entries, workspaces)
    .filter(e => (e.wsId ?? wsId) === wsId && Number.isInteger(e.v))
    .map(e => `${e.kind}#${e.v - 1}`)
    .slice(0, owner ? owner.version : undefined)
}

const inputPath = arg('input', null)
if (!inputPath) throw new Error('缺少 --input')
const doc = JSON.parse(fs.readFileSync(inputPath, 'utf-8'))
const workspaces = doc.workspaces || []
const pruned = pruneUndoneLog(doc.entries || [], doc.wsId, doc.pruneTo)
const visible = visibleActionLog(pruned, workspaces)

process.stdout.write(JSON.stringify({
  prunedIds: pruned.map(e => e.id),
  visibleIds: visible.map(e => e.id),
  visibleTitles: visible.map(e => e.title),
  browserHistory: browserHistory(pruned, workspaces, doc.wsId),
  serverHistory: serverHistory(doc.ops || [], doc.version),
  history: historyCounters(doc.meta || {})
}))
