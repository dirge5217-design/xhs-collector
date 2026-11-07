'use strict';

const http = require('http');
const { sign, init, b64Custom } = require('./xhs_sign.js');

const PORT = Number(process.argv[2] || process.env.XHS_SIGN_PORT || 18080);

// x9 只在 x6/x7 为空、x8 为 null 时是固定值
function xsCommon(a1, dsl) {
  const obj = {
    s0: 5, s1: '',
    x0: '1', x1: '4.4.3', x2: 'Windows', x3: 'xhs-pc-web', x4: '6.60.2',
    x5: a1 || '',
    x6: '', x7: '', x8: null,
    x9: -931954833,
    x10: 0, x11: 'normal',
    x12: 'null;' + String(dsl || Date.now()),
  };
  return b64Custom(JSON.stringify(obj));
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > 8 * 1024 * 1024) { reject(new Error('body too large')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

function doSign(url, data, a1) {
  const t0 = process.hrtime.bigint();
  const r = sign(url, data);
  const cost = Number(process.hrtime.bigint() - t0) / 1e6;
  return {
    'x-s': r['x-s'],
    'x-t': r['x-t'],
    'x-s-common': xsCommon(a1, Date.now()),
    cost_ms: Math.round(cost * 100) / 100,
  };
}

const t0 = Date.now();
init();
console.log(`[signer] 初始化完成 ${Date.now() - t0}ms | mnsv2 就绪`);

const server = http.createServer(async (req, res) => {
  const send = (code, obj) => {
    const body = JSON.stringify(obj);
    res.writeHead(code, {
      'Content-Type': 'application/json; charset=utf-8',
      'Content-Length': Buffer.byteLength(body),
    });
    res.end(body);
  };

  try {
    if (req.method === 'GET' && req.url === '/health') {
      return send(200, { ok: true, pid: process.pid, uptime_s: Math.round(process.uptime()) });
    }

    if (req.method === 'POST' && req.url === '/sign') {
      const raw = await readBody(req);
      const p = JSON.parse(raw || '{}');
      if (!p.url) return send(400, { error: 'url required' });
      return send(200, doSign(p.url, p.data, p.a1));
    }

    if (req.method === 'POST' && req.url === '/sign_batch') {
      const raw = await readBody(req);
      const p = JSON.parse(raw || '{}');
      const items = p.items || [];
      const results = [];
      for (const it of items) {
        try {
          results.push(doSign(it.url, it.data, it.a1 || p.a1));
        } catch (e) {
          results.push({ error: String(e.message).slice(0, 200) });
        }
      }
      return send(200, { results });
    }

    return send(404, { error: 'not found' });
  } catch (e) {
    return send(500, { error: String(e.message).slice(0, 300) });
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[signer] listening http://127.0.0.1:${PORT}`);
});
