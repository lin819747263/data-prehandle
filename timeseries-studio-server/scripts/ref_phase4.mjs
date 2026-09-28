// 第④期对拍参考实现：把迁移前浏览器里那份「整表统计 / 直方图 / 降采样曲线」的算法
// 用原生 JS 重写一遍（不依赖 pandas、不读服务端代码），由 verify_phase4.py 拿去和服务端
// /api/ws/{id}/stats、/hist、/series-multi 的输出逐格比。
//
// 它是"要被替换掉的那份实现"的口径快照，不是服务端代码的翻译：
//   · 分位数取排序后第 floor(n·0.25)/floor(n·0.75) 个观测值（不是线性插值）；
//   · 偶数个观测值取中间两个的平均；
//   · 标准差是总体标准差（÷n）；
//   · 直方图 25 桶、桶宽 (max-min)/25、末值并入最后一桶；
//   · 窗口均值步长 4、只对有效值求平均、结果 parseFloat(x.toFixed(2))、逐项累加。
//
// 用法：node ref_phase4.mjs --csv <路径> --cols a,b,c [--mode raw|extremes|mean] [--points 3000] [--bins 25]
import fs from 'node:fs'

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name)
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : dflt
}

const csvPath = arg('csv', null)
if (!csvPath) throw new Error('缺少 --csv')
const cols = (arg('cols', '') || '').split(',').filter(Boolean)
const modes = (arg('modes', 'raw,extremes,mean') || '').split(',').filter(Boolean)
const points = Number(arg('points', 3000))
const bins = Number(arg('bins', 25))

// ---------------------------------------------------------------- CSV 解析
function splitLine(line) {
  const out = []
  let cur = ''
  let quoted = false
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    if (quoted) {
      if (ch === '"') {
        if (line[i + 1] === '"') { cur += '"'; i++ } else quoted = false
      } else cur += ch
    } else if (ch === '"') quoted = true
    else if (ch === ',') { out.push(cur); cur = '' }
    else cur += ch
  }
  out.push(cur)
  return out
}

const raw = fs.readFileSync(csvPath, 'utf8').replace(/^\ufeff/, '')
const lines = raw.split(/\r?\n/).filter(l => l.length)
const header = splitLine(lines[0])
const body = lines.slice(1).map(splitLine)
const idxOf = k => header.indexOf(k)

function numberOrNaN(v) {
  if (v === undefined || v === null) return NaN
  const s = String(v).trim()
  if (s === '') return NaN
  const n = Number(s)
  return Number.isNaN(n) ? NaN : n
}

const columns = {}
for (const key of cols) {
  const i = idxOf(key)
  if (i < 0) throw new Error(`列不存在：${key}`)
  columns[key] = body.map(r => numberOrNaN(r[i]))
}
const timeCol = header[0]
const labels = body.map(r => String(r[0] ?? '').trim())
const rowCount = body.length

// ---------------------------------------------------------------- 统计
function colStats(arr) {
  const total = arr.length
  const valid = arr.filter(v => !Number.isNaN(v))
  const n = valid.length
  if (n === 0) {
    return { n: 0, missing: total, mean: null, std: null, median: null, q1: null, q3: null, min: null, max: null }
  }
  const sorted = valid.slice().sort((a, b) => a - b)
  let sum = 0
  for (const v of sorted) sum += v
  const mean = sum / n
  let sq = 0
  for (const v of sorted) sq += (v - mean) * (v - mean)
  const median = n % 2 ? sorted[(n - 1) >> 1] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2
  return {
    n, missing: total - n, mean, std: Math.sqrt(sq / n), median,
    q1: sorted[Math.floor(n * 0.25)], q3: sorted[Math.floor(n * 0.75)],
    min: sorted[0], max: sorted[n - 1],
  }
}

function statsMatrix() {
  let globalMax = 1
  const rows = cols.map(key => {
    const s = colStats(columns[key])
    if (s.n && s.max !== null && s.max > globalMax) globalMax = s.max
    return { key, ...s, missingRate: rowCount ? s.missing / rowCount * 100 : 0 }
  })
  return { rowCount, colCount: cols.length, globalMax, rows }
}

function histogram(key) {
  const arr = columns[key]
  const valid = arr.filter(v => !Number.isNaN(v)).sort((a, b) => a - b)
  const n = valid.length
  const out = { col: key, bins, n, missing: arr.length - n, totalRows: arr.length,
                min: null, max: null, mean: null, median: null, binWidth: null,
                edges: [], counts: [], meanBin: null, medianBin: null }
  if (!n) return out
  const lo = valid[0], hi = valid[n - 1]
  const width = (hi - lo) / bins || 1
  const counts = new Array(bins).fill(0)
  for (const v of valid) {
    let i = Math.floor((v - lo) / width)
    if (i < 0) i = 0
    if (i > bins - 1) i = bins - 1
    counts[i]++
  }
  let sum = 0
  for (const v of valid) sum += v
  const mean = sum / n
  const median = n % 2 ? valid[(n - 1) >> 1] : (valid[n / 2 - 1] + valid[n / 2]) / 2
  const span = (hi - lo) || 1
  const clamp = i => Math.min(bins - 1, Math.max(0, Math.floor(i)))
  out.min = lo; out.max = hi; out.mean = mean; out.median = median; out.binWidth = width
  out.edges = Array.from({ length: bins }, (_, i) => lo + i * width)
  out.counts = counts
  out.meanBin = clamp((mean - lo) / span * bins)
  out.medianBin = clamp((median - lo) / span * bins)
  return out
}

// ---------------------------------------------------------------- 降采样
function stride(positions, cap) {
  if (positions.length <= cap) return positions
  const step = Math.ceil(positions.length / cap)
  const kept = positions.filter((_, i) => i % step === 0)
  if (kept[kept.length - 1] !== positions[positions.length - 1]) kept.push(positions[positions.length - 1])
  return kept
}

function envelope(arr, maxPoints) {
  const n = arr.length
  if (n <= maxPoints) return Array.from({ length: n }, (_, i) => i)
  const buckets = Math.max(1, Math.min(n, Math.floor(maxPoints / 4)))
  const keep = new Set()
  for (let b = 0; b < buckets; b++) {
    const a = Math.floor(b * n / buckets)
    const e = Math.max(a + 1, Math.floor((b + 1) * n / buckets))
    let mini = -1, maxi = -1, firstNaN = -1, lastNaN = -1
    for (let i = a; i < e; i++) {
      if (Number.isNaN(arr[i])) { if (firstNaN < 0) firstNaN = i; lastNaN = i; continue }
      if (mini < 0 || arr[i] < arr[mini]) mini = i
      if (maxi < 0 || arr[i] > arr[maxi]) maxi = i
    }
    if (mini >= 0) { keep.add(mini); keep.add(maxi) }
    if (firstNaN >= 0) { keep.add(firstNaN); keep.add(lastNaN) }
  }
  return [...keep].sort((a, b) => a - b)
}

function meanChunks(arr, positions, step) {
  return positions.map(start => {
    const valid = arr.slice(start, start + step).filter(v => !Number.isNaN(v))
    if (!valid.length) return null
    let total = 0
    for (const v of valid) total += v
    return Number((total / valid.length).toFixed(2))
  })
}

// LTTB 的独立实现（与 app/services/explore.py 同一份规格，代码各写一遍）：
// 缺失值代入本列有效均值只用于选点，极差用于把各列面积归一后再相加。
function lttb(cols, columns, threshold) {
  const n = columns[cols[0]].length
  if (n <= threshold || threshold < 3) return Array.from({ length: n }, (_, i) => i)
  const prep = cols.map(key => {
    const arr = columns[key]
    const valid = arr.filter(v => !Number.isNaN(v))
    const mean = valid.length ? valid.reduce((a, b) => a + b, 0) / valid.length : 0
    const span = valid.length ? Math.max(...valid) - Math.min(...valid) : 0
    return { arr: arr.map(v => (Number.isNaN(v) ? mean : v)), norm: span || 1 }
  })
  const keep = [0]
  const every = (n - 2) / (threshold - 2)
  let a = 0
  for (let i = 0; i < threshold - 2; i++) {
    const curS = Math.floor(i * every) + 1
    const curE = Math.min(Math.floor((i + 1) * every) + 1, n)
    let nxtS = curE
    let nxtE = Math.min(Math.floor((i + 2) * every) + 1, n)
    if (nxtE <= nxtS) nxtE = Math.min(nxtS + 1, n)
    if (curE <= curS || nxtE <= nxtS || curS >= n) break
    let sumX = 0, sumY = prep.map(() => 0)
    for (let j = nxtS; j < nxtE; j++) {
      sumX += j
      prep.forEach((p, ci) => { sumY[ci] += p.arr[j] })
    }
    const cnt = nxtE - nxtS
    const avgX = sumX / cnt
    const avgY = sumY.map(v => v / cnt)
    let best = curS, bestScore = -1
    for (let j = curS; j < curE; j++) {
      let score = 0
      prep.forEach((p, ci) => {
        const ay = p.arr[a]
        score += Math.abs((a - avgX) * (p.arr[j] - ay) - (a - j) * (avgY[ci] - ay)) / p.norm
      })
      if (score > bestScore) { bestScore = score; best = j }
    }
    a = best
    keep.push(a)
  }
  keep.push(n - 1)
  return keep
}

function seriesMulti(mode) {
  const cap = Math.max(20, Math.min(points, 6000))
  let step = 1
  let positions
  if (!rowCount) positions = []
  else if (mode === 'mean') {
    step = rowCount > 500 ? 4 : 1
    positions = []
    for (let i = 0; i < rowCount; i += step) positions.push(i)
  } else if (mode === 'raw') {
    // 全量：窗口内每一行都留下，不做任何抽取
    positions = Array.from({ length: rowCount }, (_, i) => i)
  } else if (mode === 'lttb') {
    positions = lttb(cols, columns, cap)
  } else {
    const budget = Math.max(200, Math.floor(cap / cols.length))
    const keep = new Set()
    for (const key of cols) for (const p of envelope(columns[key], budget)) keep.add(p)
    positions = stride([...keep].sort((a, b) => a - b), cap)
  }
  const series = cols.map(key => {
    const arr = columns[key]
    const ys = step > 1 ? meanChunks(arr, positions, step)
      : positions.map(i => (Number.isNaN(arr[i]) ? null : arr[i]))
    return { col: key, y: ys, missing: ys.filter(v => v === null).length }
  })
  return { mode, rowCount, points: positions.length, windowStep: step,
           maxPoints: mode === 'raw' ? null : cap,
           decimated: positions.length < rowCount,
           x: positions.map(i => labels[i] ?? String(i)), series }
}

const payload = {
  csv: csvPath, timeCol, rowCount, cols,
  stats: statsMatrix(),
  hist: cols.map(histogram),
  series: modes.map(seriesMulti),
}
process.stdout.write(JSON.stringify(payload))
