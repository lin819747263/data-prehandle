// LTTB 的参照实现（纯 JS，独立于 app/services/explore.py 另写一遍）。
// 输入：stdin 的 {"cols": [[数值|null,...], ...], "threshold": 300}
// 输出：stdout 的 {"positions": [选中行的下标...]}
//
// 规格与服务端一致，代码各写一遍，这样两边对上才算证据：
// - 桶边界：every = (n-2)/(threshold-2)，第 i 个桶取 [floor(i*every)+1, floor((i+1)*every)+1)
// - 选点判据：与「已选点 a」和「下一桶均值点」构成的三角形面积；多列时把每列面积除以该列极差再相加
//   （极差归一才能让量纲不同的列相加，顺带保证结果不取决于先勾了哪一列；单列时它就是教科书 LTTB）
// - 缺失值：面积计算里代入本列有效均值，极差也只用有效值算
// - 并列取靠前的那个（`>` 比较）
import fs from "node:fs";

const buf = fs.readFileSync(0, "utf8");
const { cols, threshold } = JSON.parse(buf);
const n = cols[0].length;

if (n <= threshold || threshold < 3) {
  process.stdout.write(JSON.stringify({ positions: Array.from({ length: n }, (_, i) => i) }));
} else {
  const prepared = cols.map(values => {
    let sum = 0, cnt = 0, lo = Infinity, hi = -Infinity;
    for (const v of values) {
      if (v === null || v === undefined || Number.isNaN(v)) continue;
      sum += v; cnt += 1;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    const mean = cnt ? sum / cnt : 0;
    const range = cnt ? (hi - lo || 1) : 1;
    return { y: values.map(v => (v === null || v === undefined || Number.isNaN(v) ? mean : v)), range };
  });

  const positions = [0];
  const every = (n - 2) / (threshold - 2);
  let a = 0;
  for (let i = 0; i < threshold - 2; i++) {
    const curS = Math.floor(i * every) + 1;
    const curE = Math.min(Math.floor((i + 1) * every) + 1, n);
    const nxtS = curE;
    let nxtE = Math.min(Math.floor((i + 2) * every) + 1, n);
    if (nxtE <= nxtS) nxtE = Math.min(nxtS + 1, n);
    if (curE <= curS || nxtE <= nxtS || curS >= n) break;

    const bucketCount = nxtE - nxtS;
    let sumX = 0;
    const sumY = prepared.map(() => 0);
    for (let j = nxtS; j < nxtE; j++) {
      sumX += j;
      for (let c = 0; c < prepared.length; c++) sumY[c] += prepared[c].y[j];
    }
    const avgX = sumX / bucketCount;
    const avgY = sumY.map(v => v / bucketCount);

    let bestIdx = curS, bestScore = -1;
    for (let j = curS; j < curE; j++) {
      let score = 0;
      for (let c = 0; c < prepared.length; c++) {
        const col = prepared[c];
        const ay = col.y[a];
        score += Math.abs((a - avgX) * (col.y[j] - ay) - (a - j) * (avgY[c] - ay)) / col.range;
      }
      if (score > bestScore) { bestScore = score; bestIdx = j; }
    }
    a = bestIdx;
    positions.push(a);
  }
  positions.push(n - 1);
  process.stdout.write(JSON.stringify({ positions }));
}
