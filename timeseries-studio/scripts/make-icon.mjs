// 从应用自己的视觉标识生成 electron 图标（electron/icon.ico）。
// 画的是闪屏/工作台头部那个 mark：#4f46e5 圆角方块 + 24 格坐标系里的白色折线（path: M3 17 l5 -6 l4 3 l5 -8 l4 5）。
// 无第三方依赖：4x 超采样光栅化 -> 手写 PNG 编码器 -> 多尺寸 ICO 容器。
import { deflateSync } from 'node:zlib'
import { writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const OUT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'electron', 'icon.ico')
const SIZES = [256, 128, 64, 48, 32, 16]
const PRIMARY = [0x4f, 0x46, 0xe5]
const INK = [255, 255, 255]

// 折线顶点（24x24 viewBox 坐标）
const PTS = [[3, 17], [8, 11], [12, 14], [17, 6], [21, 11]]
const MARK_BOX = 0.62      // 24 格内容在图标里占的边长比例
const STROKE = 2.4         // 原始描边宽度（24 格单位）

const lerp = (a, b, t) => a + (b - a) * t

/** 点到线段的距离 */
function distToSeg(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay
  const len2 = dx * dx + dy * dy
  const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2))
  const cx = ax + t * dx, cy = ay + t * dy
  return Math.hypot(px - cx, py - cy)
}

/** 圆角矩形内距（外部为正，内部为负） */
function sdRoundRect(x, y, size, r) {
  const half = size / 2
  const qx = Math.abs(x - half) - (half - r)
  const qy = Math.abs(y - half) - (half - r)
  const outside = Math.hypot(Math.max(qx, 0), Math.max(qy, 0))
  return outside + Math.min(Math.max(qx, qy), 0) - r
}

/** 渲染一个 size x size 的 RGBA 画布（sample 倍超采样后做面积平均） */
function render(size) {
  const sample = 4
  const n = size * sample
  const scale = n / size
  const box = MARK_BOX * n
  const origin = (n - box) / 2
  const toPx = ([x, y]) => [origin + (x / 24) * box, origin + (y / 24) * box]
  const segs = []
  for (let i = 0; i < PTS.length - 1; i++) segs.push([...toPx(PTS[i]), ...toPx(PTS[i + 1])])
  // 小尺寸做光学补偿：折线加粗，圆帽/圆角本来就由距离场给出
  const stroke = (STROKE / 24) * box * (size <= 32 ? 1.35 : 1)
  const radius = (8 / 32) * n

  const rgba = new Uint8Array(size * size * 4)
  for (let py = 0; py < size; py++) {
    for (let px = 0; px < size; px++) {
      let covBg = 0, covInk = 0
      for (let sy = 0; sy < sample; sy++) {
        for (let sx = 0; sx < sample; sx++) {
          const fx = (px + sx / sample) * scale
          const fy = (py + sy / sample) * scale
          if (sdRoundRect(fx, fy, n, radius) > 0) continue
          covBg++
          let d = Infinity
          for (const s of segs) d = Math.min(d, distToSeg(fx, fy, s[0], s[1], s[2], s[3]))
          if (d <= stroke / 2) covInk++
        }
      }
      const o = (py * size + px) * 4
      const aBg = covBg / (sample * sample)
      if (aBg === 0) continue
      const aInk = covInk / (sample * sample)
      const t = aInk
      rgba[o] = Math.round(lerp(PRIMARY[0], INK[0], t))
      rgba[o + 1] = Math.round(lerp(PRIMARY[1], INK[1], t))
      rgba[o + 2] = Math.round(lerp(PRIMARY[2], INK[2], t))
      rgba[o + 3] = Math.round(aBg * 255)
    }
  }
  return rgba
}

const CRC_TABLE = (() => {
  const t = new Int32Array(256)
  for (let i = 0; i < 256; i++) {
    let c = i
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    t[i] = c
  }
  return t
})()

function crc32(buf) {
  let c = 0xffffffff
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

function chunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(body))
  return Buffer.concat([len, body, crc])
}

function encodePng(size, rgba) {
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(size, 0)
  ihdr.writeUInt32BE(size, 4)
  ihdr[8] = 8   // bit depth
  ihdr[9] = 6   // RGBA
  const raw = Buffer.alloc((size * 4 + 1) * size)
  for (let y = 0; y < size; y++) {
    raw[y * (size * 4 + 1)] = 0
    raw.set(rgba.subarray(y * size * 4, (y + 1) * size * 4), y * (size * 4 + 1) + 1)
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0))
  ])
}

/** ICO 容器：Vista 起支持条目内直接放 PNG */
function encodeIco(images) {
  const header = Buffer.alloc(6)
  header.writeUInt16LE(0, 0)
  header.writeUInt16LE(1, 2)
  header.writeUInt16LE(images.length, 4)
  let offset = 6 + 16 * images.length
  const dirs = images.map(({ size, buf }) => {
    const d = Buffer.alloc(16)
    d[0] = size >= 256 ? 0 : size
    d[1] = size >= 256 ? 0 : size
    d[4] = 1          // planes
    d[6] = 32         // bpp
    d.writeUInt32LE(buf.length, 8)
    d.writeUInt32LE(offset, 12)
    offset += buf.length
    return d
  })
  return Buffer.concat([header, ...dirs, ...images.map((i) => i.buf)])
}

const images = SIZES.map((size) => ({ size, buf: encodePng(size, render(size)) }))
writeFileSync(OUT, encodeIco(images))
console.log(`图标已生成：${OUT}`)
for (const im of images) console.log(`  ${im.size}x${im.size} PNG ${im.buf.length} 字节`)
