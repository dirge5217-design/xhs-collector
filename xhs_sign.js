'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { JSDOM } = require('jsdom');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36';

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

let _win = null;

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

  try {
    Object.defineProperty(w.navigator, 'webdriver', { get: () => false, configurable: true });
    Object.defineProperty(w.navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
    Object.defineProperty(w.navigator, 'deviceMemory', { get: () => 8, configurable: true });
    Object.defineProperty(w.navigator, 'languages', { get: () => ['zh-CN', 'zh'], configurable: true });
  } catch (e) { /* 部分 navigator 属性不可重定义 */ }

  w.performance.now = () => Date.now();

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
  // InstallTrigger 必须留 undefined

  // 保留真实 canvas 节点，只补 2D 上下文
  if (w.HTMLCanvasElement) {
    w.HTMLCanvasElement.prototype.getContext = function (kind) {
      if (kind === 'webgl' || kind === 'experimental-webgl' || kind === 'webgl2') return null;
      return makeCanvasCtx();
    };
    w.HTMLCanvasElement.prototype.toDataURL = function () {
      return 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
    };
  }

  // 载荷会传非 Node 参数做环境探测，严格校验会挡住它
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
  const S = {
    x0: '4.4.3',
    x1: 'xhs-pc-web',
    x2: 'Windows',
    x3: w.mnsv2(c, u, md5hex(url)),
    x4: x4,
    x5: u,
  };

  return {
    'x-s': 'XYS_' + b64Custom(JSON.stringify(S)),
    'x-t': String(Date.now()),
  };
}

module.exports = { sign, b64Custom, init };
