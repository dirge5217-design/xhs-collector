/**
 * 小红书 x-s / x-t 签名器（纯 Node，零浏览器）
 *
 * 依赖：
 *   - jsdom        提供真实 DOM（VM 会做 appendChild/removeChild，假对象过不了类型校验）
 *   - _eval_captured.js  signV2Init 实际 eval 的载荷（288KB，含字节码 VM）
 *
 * 算法（来自 vendor-dynamic.js 的 seccore_signv2，已逐字段核对）：
 *   c = url + (data 为 object ? JSON.stringify(data) : data 为 string ? data : "")
 *   u = md5(c); p = md5(url)
 *   x3 = window.mnsv2(c, u, p)
 *   S = {x0:"4.4.3", x1:"xhs-pc-web", x2:"Windows", x3, x4: data?typeof data:"", x5:u}
 *   X-s = "XYS_" + base64_custom( utf8(JSON.stringify(S)) )
 *
 * 用法：
 *   node xhs_sign.js '/api/sns/web/v1/feed' '{"source_note_id":"..."}'
 *   -> {"x-s":"XYS_...","x-t":"1794..."}
 */
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { JSDOM } = require('jsdom');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36';

// 自定义 base64 字母表（来自 bundle 模块 384）
const CUSTOM_ALPHABET = 'ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5';
const STD_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

const md5hex = (s) => crypto.createHash('md5').update(s, 'utf8').digest('hex');

function b64Custom(str) {
  const std = Buffer.from(str, 'utf8').toString('base64');
  let out = '';
  for (const ch of std) {
    if (ch === '=') { out += '='; continue; }
    const i = STD_ALPHABET.indexOf(ch);
    out += i >= 0 ? CUSTOM_ALPHABET[i] : ch;
  }
  return out;
}

// 反向：解码（调试用）
function b64CustomDecode(s) {
  let std = '';
  for (const ch of s) {
    if (ch === '=') { std += '='; continue; }
    const i = CUSTOM_ALPHABET.indexOf(ch);
    std += i >= 0 ? STD_ALPHABET[i] : ch;
  }
  return Buffer.from(std, 'base64').toString('utf8');
}

let _win = null;

function makeCanvasCtx() {
  const noop = () => {};
  const o = {
    fillStyle: '#000', strokeStyle: '#000', globalAlpha: 1, lineWidth: 1,
    font: '10px sans-serif', textBaseline: 'alphabetic', textAlign: 'start',
    globalCompositeOperation: 'source-over', shadowBlur: 0, shadowColor: '#000',
    measureText: (t) => ({
      width: String(t).length * 6,
      actualBoundingBoxAscent: 8, actualBoundingBoxDescent: 2,
    }),
    getImageData: (x, y, w, h) => ({
      data: new Uint8ClampedArray(Math.max(4, (w || 1) * (h || 1) * 4)),
      width: w, height: h,
    }),
    createImageData: (w, h) => ({
      data: new Uint8ClampedArray(Math.max(4, (w || 1) * (h || 1) * 4)),
      width: w, height: h,
    }),
    createLinearGradient: () => ({ addColorStop: noop }),
    createRadialGradient: () => ({ addColorStop: noop }),
    createPattern: () => ({}),
  };
  for (const m of ['fillRect', 'clearRect', 'strokeRect', 'beginPath', 'closePath',
    'moveTo', 'lineTo', 'arc', 'arcTo', 'rect', 'bezierCurveTo', 'quadraticCurveTo',
    'ellipse', 'fill', 'stroke', 'clip', 'save', 'restore', 'translate', 'rotate',
    'scale', 'transform', 'setTransform', 'resetTransform', 'setLineDash',
    'fillText', 'strokeText', 'drawImage', 'putImageData']) o[m] = noop;
  o.getLineDash = () => [];
  return o;
}

function init() {
  if (_win) return _win;

  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', {
    url: 'https://www.xiaohongshu.com/explore',
    referrer: 'https://www.xiaohongshu.com/',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    userAgent: UA,
  });
  const w = dom.window;

  // navigator 关键项
  try {
    Object.defineProperty(w.navigator, 'webdriver', { get: () => false, configurable: true });
    Object.defineProperty(w.navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
    Object.defineProperty(w.navigator, 'deviceMemory', { get: () => 8, configurable: true });
    Object.defineProperty(w.navigator, 'languages', { get: () => ['zh-CN', 'zh'], configurable: true });
  } catch (e) { /* ignore */ }

  w.performance.now = () => Date.now() - 0;

  // 浏览器专有对象（依赖数组里明确列了 chrome / insight）
  w.chrome = {
    app: { isInstalled: false, InstallState: {}, RunningState: {} },
    runtime: {},
    csi: () => ({ startE: 0, onloadT: Date.now(), pageT: 120.5, tran: 15 }),
    loadTimes: () => ({
      requestTime: 0, startLoadTime: 0, commitLoadTime: 0,
      finishDocumentLoadTime: 0, finishLoadTime: 0, firstPaintTime: 0,
      firstPaintAfterLoadTime: 0, navigationType: 'Other',
      wasFetchedViaSpdy: true, wasNpnNegotiated: true,
      npnNegotiatedProtocol: 'h2', wasAlternateProtocolAvailable: false,
      connectionInfo: 'h2',
    }),
  };
  if (typeof w.insight === 'undefined') w.insight = {};
  // InstallTrigger 必须保持 undefined（Firefox 专有）

  // canvas：保留 jsdom 真实节点，只打补丁
  if (w.HTMLCanvasElement) {
    w.HTMLCanvasElement.prototype.getContext = function (kind) {
      if (kind === 'webgl' || kind === 'experimental-webgl' || kind === 'webgl2') return null;
      return makeCanvasCtx();
    };
    w.HTMLCanvasElement.prototype.toDataURL = function () {
      return 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
    };
  }

  // DOM 操作宽容化：VM 会传非 Node 参数做探测
  for (const m of ['removeChild', 'appendChild', 'insertBefore', 'replaceChild']) {
    const proto = w.Node.prototype;
    const orig = proto[m];
    if (typeof orig === 'function') {
      proto[m] = function (c) {
        try { return orig.call(this, c); } catch (e) { return c; }
      };
    }
  }

  const payload = fs.readFileSync(path.join(__dirname, '_eval_captured.js'), 'utf8');
  w.eval(payload);

  if (typeof w.mnsv2 !== 'function') {
    throw new Error('mnsv2 未挂载：载荷或宿主环境不匹配');
  }
  _win = w;
  return w;
}

/**
 * 生成签名头
 * @param {string} url   接口路径（含 query），如 /api/sns/web/v1/feed
 * @param {*} data       请求体（对象/字符串/空）
 * @returns {{ 'x-s': string, 'x-t': string }}
 */
function sign(url, data) {
  const w = init();

  let c = url;
  let x4 = '';
  if (data !== undefined && data !== null) {
    if (typeof data === 'object') {
      c += JSON.stringify(data);
      x4 = 'object';
    } else if (typeof data === 'string') {
      if (data) { c += data; x4 = 'string'; }
    }
  }

  const u = md5hex(c);
  const p = md5hex(url);
  const x3 = w.mnsv2(c, u, p);

  const S = {
    x0: '4.4.3',
    x1: 'xhs-pc-web',
    x2: 'Windows',
    x3: x3,
    x4: x4,
    x5: u,
  };

  return {
    'x-s': 'XYS_' + b64Custom(JSON.stringify(S)),
    'x-t': String(Date.now()),
    // 调试用
    _debug: { x3, x5: u, x4, c_len: c.length },
  };
}

module.exports = { sign, b64Custom, b64CustomDecode, init };

if (require.main === module) {
  const url = process.argv[2];
  const raw = process.argv[3];
  if (!url) {
    console.error('用法: node xhs_sign.js <url_path> [json_body]');
    process.exit(1);
  }
  let data;
  if (raw) {
    try { data = JSON.parse(raw); } catch (e) { data = raw; }
  }
  const t0 = Date.now();
  const r = sign(url, data);
  const cost = Date.now() - t0;
  console.log(JSON.stringify({ 'x-s': r['x-s'], 'x-t': r['x-t'], cost_ms: cost }, null, 1));
}
