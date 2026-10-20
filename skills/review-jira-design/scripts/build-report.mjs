#!/usr/bin/env node
/*
 * build-report.mjs — 由「内容 JSON + PRD + 模板」生成 G1 准入审核报告 HTML
 *
 * 用法：
 *   node build-report.mjs --prd <PRD.md> --dump-index
 *       打印 PRD 编号索引（编号 → 正式名称 / 章节名），供撰写内容时对照
 *   node build-report.mjs --prd <PRD.md> --content <content.json> --out <report.html>
 *       [--template <report-template.html>]
 *       生成自包含 HTML 报告
 *
 * 设计要点（改脚本前先读这段）：
 *   1. 正文一律不出现编号：所有 [A-Z]{2,4}-\d{3} 与 §x.y 都被替换成可读名称，
 *      编号只存在于 .ref 的 <template class="tip-tpl"> 浮层里。
 *   2. 浮层内容必须放在 <template> 里，不能内联在锚点中——
 *      锚点常位于 <h3>/<p> 内，这些元素的内容模型不接受块级元素。
 *   3. 浮层内容按三种形态分点：编号列表 / 键值段落（关联功能、前置条件、WHEN、THEN）/ 句子条。
 *   4. 一致性格固定「不一致在前、一致在后」。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_TEMPLATE = path.resolve(HERE, '..', 'references', 'report-template.html');

/* ══════════════════════════ 基础工具 ══════════════════════════ */
const txt = (h) => String(h == null ? '' : h).replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
const esc = (t) => String(t == null ? '' : t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const escRe = (t) => String(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const PUA0 = '\uE000', PUA1 = '\uE001';

const CAT = { FR: '功能要求', BR: '业务规则', AC: '验收条件', D: '产品决策', G: '需求目标', IR: '交互规则' };
const LEVEL_NAME = { bad: '阻断', warn: '需补齐', info: '建议', ok: '一致', pending: '待判断' };
const TONE = { pass: 'pass', conditional: 'conditional', return: 'return', pending: 'pending' };
/* 映射表「结论」 → 级别（用于配色与默认过滤） */
const MAP_LEVEL = { 一致: 'ok', 部分: 'warn', 冲突: 'bad', 缺失: 'bad', 未验证: 'info', 建议: 'info', 复用: 'reuse', 待判断: 'pending', 待确认: 'pending' };
const mapLevel = (v) => {
  const t = String(v || '');
  /* 复用项按 SKILL.md 3.1 不判：不进不一致，也不参与阻断/需补齐计数 */
  if (t.includes('复用')) return t.includes('待') ? 'pending' : 'reuse';
  if (t.includes('待判断') || t.includes('待确认')) return 'pending';
  if (t.includes('一致') && !t.includes('不一致')) return 'ok';
  if (t.includes('不一致') || t.includes('冲突')) return 'bad';
  if (t.includes('缺失')) return 'bad';
  if (t.includes('未验证')) return 'info';
  if (t.includes('部分')) return 'warn';
  return MAP_LEVEL[t] || 'ok';
};

/* ══════════════════════════ PRD 索引 ══════════════════════════ */
const GENERIC_LEAF = ['数据', '状态', '异常', '交互', '业务规则', '产品逻辑', '前置与触发',
  '用户可见文案', '需求说明', '核心测试场景', '决策台账'];
const PRD_IDS = {}, PRD_SEC = {}, PRD_TEXT = {}, PRD_DETAIL = {};
const STOPWORD = ['阶段校验', '两阶段校验', '状态表', '状态表处理中行', '矩阵', '前置条件',
  '两组前置条件', '等', '第', '两', '各'];

function grabText(lines, i, n) {
  const out = [];
  for (let j = i + 1; j < lines.length && out.join('').length < n; j += 1) {
    const l = lines[j].trim();
    if (/^#{2,5}\s/.test(l)) break;
    if (/^[-*\s]*\*\*[A-Z]{2,4}-\d{3}/.test(l)) break;
    if (/^(关联功能|关联规则|关联)\s*[：:]/.test(l)) continue;
    if (/^\|/.test(l)) continue;
    if (!l) continue;
    out.push(l.replace(/^[-*]\s*/, '').replace(/\*\*/g, ''));
  }
  return txt(out.join(' ')).slice(0, n);
}

function buildPrdIndex(prdPath) {
  const lines = fs.readFileSync(prdPath, 'utf8').split(/\r?\n/);
  const secTitle = {};
  lines.forEach((ln, i) => {
    let m = /^#{2,5}\s*(\d+(?:\.\d+)*)\.?\s+(.*)$/.exec(ln);
    if (m) {
      let t = txt(m[2]);
      const im = /^([A-Z]{2,4}-\d{3})[：:\s]+(.+)$/.exec(t);
      if (im) {
        if (!PRD_IDS[im[1]]) PRD_IDS[im[1]] = txt(im[2]);
        PRD_TEXT[im[1]] = PRD_TEXT[im[1]] || grabText(lines, i, 1200);
        t = txt(im[2]);
      }
      secTitle[m[1]] = t;
      if (m[1].includes('.')) {
        PRD_TEXT['§' + m[1]] = PRD_TEXT['§' + m[1]] || grabText(lines, i, 1200);
      }
    }
    m = /^#{3,5}\s*(\d+(?:\.\d+)*)\s+([A-Z]{2,4}-\d{3})[：:\s]\s*(.+)$/.exec(ln);
    if (m) {
      PRD_IDS[m[2]] = PRD_IDS[m[2]] || txt(m[3]);
      PRD_TEXT[m[2]] = PRD_TEXT[m[2]] || grabText(lines, i, 1200);
    }
    m = /^[-*\s]*\*\*([A-Z]{2,4}-\d{3})[：:]\s*([^*]+)\*\*/.exec(ln);
    if (m) {
      PRD_IDS[m[1]] = PRD_IDS[m[1]] || txt(m[2]);
      PRD_TEXT[m[1]] = PRD_TEXT[m[1]] || grabText(lines, i, 1200);
    }
    m = /^\|\s*([A-Z]{2,4}-\d{3})\s*\|\s*([^|]+?)\s*\|/.exec(ln);
    if (m && !PRD_IDS[m[1]]) PRD_IDS[m[1]] = txt(m[2]);
  });
  Object.keys(secTitle).forEach((k) => {
    const leaf = secTitle[k];
    if (GENERIC_LEAF.indexOf(leaf) >= 0) {
      const pid = k.split('.').slice(0, -1).join('.');
      const pm = /^[A-Z]{2,4}-\d{3}[：:]\s*(.+)$/.exec(secTitle[pid] || '');
      let ps = pm ? pm[1] : (secTitle[pid] || '');
      ps = ps.replace(/^智能体/, '').replace(/^及/, '').replace(/^的/, '').replace(/规则$/, '');
      PRD_SEC[k] = ps || leaf;
    } else PRD_SEC[k] = leaf;
  });
}

const titleOf = (id) => {
  id = String(id).split(/\s*[—–~]\s*/)[0];
  if (/^§/.test(id)) return PRD_SEC[id.slice(1)] || '';
  return PRD_IDS[id] || '';
};
const detailOf = (id) => {
  id = String(id).split(/\s*[—–~]\s*/)[0];
  const raw = id.replace(/^§/, '');
  return PRD_DETAIL[id] || PRD_TEXT[raw] || PRD_TEXT[id] || '';
};

/* ═══════════════════ 可读名称：从依据里取「上下文短名」 ═══════════════════ */
function usableDesc(d) {
  if (!d) return '';
  let t = String(d).replace(/^[\s、，,：:（(]*/, '').replace(/[\s、，,：:）)。；]*$/, '');
  t = t.replace(/^[“”"‘’「」『』]+/, '').replace(/[“”"‘’「」『』]+$/, '');
  if (/^(第|等|两|各|与|和|或)/.test(t)) t = t.replace(/^(第|等|两|各|与|和|或)/, '');
  if (t.length < 2 || t.length > 12) return '';
  if (/[0-9]/.test(t)) return '';
  if (STOPWORD.indexOf(t) >= 0) return '';
  if (/^(组|条|项|处|个|两|前|后)/.test(t)) return '';
  if (/^(前置条件|后置条件|各条|首行|两组|前置)$/.test(t)) return '';
  if (/^(状态表|矩阵|其余|其他|及其)/.test(t)) return '';
  return t;
}
function ctxNames(依据) {
  const out = {};
  const t = txt(依据);
  [...t.matchAll(/([A-Z]{2,4}-\d{3})\s*([^、，,。；;]*)/g)].forEach((x) => {
    const d = usableDesc(x[2]);
    if (d && !out[x[1]]) out[x[1]] = d;
  });
  [...t.matchAll(/§(\d+(?:\.\d+)*)\s*([^、，,。；;（(]*)/g)].forEach((x) => {
    const k = x[1];
    const d = usableDesc(x[2]);
    if (!out['§' + k]) out['§' + k] = d || PRD_SEC[k] || '';
  });
  [...t.matchAll(/(?<![\w.§])(\d+\.\d+(?:\.\d+)?)(?![\d.])/g)].forEach((x) => {
    if (PRD_SEC[x[1]] && !out['§' + x[1]]) out['§' + x[1]] = PRD_SEC[x[1]];
  });
  return out;
}

const SCOPED = /^(的|之|中|下|内|等|前置|后置|对应|相应|要求|规定|定义|直接|相关)/;
const JOINER = /[及和与或、]$/;
function labelOf(id, ctx, after) {
  const own = ctx && ctx[id];
  const title = titleOf(id);
  const cat = CAT[String(id).split('-')[0]] || 'PRD 对应要求';
  if (own && !SCOPED.test(after || '')) return own;
  if (id.slice(0, 2) === 'BR' && title && !SCOPED.test(after || '')) {
    return /^文案/.test(after || '') ? title.replace(/规则$/, '') : title;
  }
  if (id.slice(0, 2) === 'AC') return '验收条件';
  if (id.slice(0, 2) === 'BR') return '业务规则';
  if (title && !SCOPED.test(after || '')) return title.replace(/规则$/, '');
  return cat;
}
function rangeLabel(a, b) {
  const pa = a.slice(0, 2), pb = b.slice(0, 2);
  if (pa !== pb) return '';
  const cat = CAT[pa] || 'PRD 条目';
  const nums = Object.keys(PRD_IDS).filter((k) => k.slice(0, 2) === pa)
    .map((k) => parseInt(k.slice(3), 10)).sort((x, y) => x - y);
  const n1 = parseInt(a.slice(3), 10), n2 = parseInt(b.slice(3), 10);
  if (nums.length && nums[0] === n1 && nums[nums.length - 1] === n2) return '全部' + cat;
  return cat + '（第 ' + n1 + '–' + n2 + ' 项）';
}

/* ═══════════════════ 浮层内容：三种形态分点 ═══════════════════ */
const TIP_LABEL = /(WHEN|THEN|关联功能|关联规则|关联|前置条件|后置条件|触发与作用域|触发|作用域|进入与默认|操作与联动|处理中|成功后|取消\/关闭\/返回|取消确认|失败\/冲突|异常处理|用户可见文案|业务规则|需求说明|产品逻辑|交互|状态|数据|验收标准|后台|技术补充)[：:]/g;
function tipContent(detail) {
  let t = txt(detail || '').replace(/\s+/g, ' ').trim();
  if (!t) return '<p class="tp">PRD 中与该编号对应的条目</p>';
  if (t.length > 2400) t = t.slice(0, 2400) + '…';
  const ol = [...t.matchAll(/(?:^|\s)(\d{1,2})[.、．]\s*/g)];
  if (ol.length >= 2) {
    const items = [];
    ol.forEach((m, i) => {
      const a = m.index + m[0].length;
      const b = (i + 1 < ol.length) ? ol[i + 1].index : t.length;
      const seg = t.slice(a, b).trim().replace(/[；;]$/, '');
      if (seg) items.push(seg);
    });
    if (items.length >= 2) {
      return '<ol class="tlist">' + items.map((x) => '<li>' + esc(x) + '</li>').join('') + '</ol>';
    }
  }
  const hits = [...t.matchAll(TIP_LABEL)];
  if (hits.length) {
    const rows = [];
    const head = t.slice(0, hits[0].index).trim();
    if (head) rows.push({ k: '', v: head });
    hits.forEach((m, i) => {
      const a = m.index + m[0].length;
      const b = (i + 1 < hits.length) ? hits[i + 1].index : t.length;
      const v = t.slice(a, b).trim().replace(/^[；;、]\s*/, '').replace(/[；;]\s*$/, '');
      rows.push({ k: m[1], v });
    });
    const real = rows.filter((r) => r.v);
    if (real.length >= 2) {
      return real.map((r) => r.k
        ? '<div class="lr"><span class="lk">' + esc(r.k) + '</span><span class="lv">' + esc(r.v) + '</span></div>'
        : '<p class="tp">' + esc(r.v) + '</p>').join('');
    }
  }
  const parts = t.split(/(?<=[。；])/).map((x) => x.trim()).filter(Boolean);
  if (parts.length >= 2) return '<ul class="tlist">' + parts.map((x) => '<li>' + esc(x) + '</li>').join('') + '</ul>';
  return '<p class="tp">' + esc(t) + '</p>';
}
const tipLines = (list) => (!list.length ? ''
  : '<ul class="tlist">' + list.map((x) => '<li>' + esc(x) + '</li>').join('') + '</ul>');
const tipBox = (hint, inner) => '<template class="tip-tpl"><b>' + esc(hint) + '</b>' + (inner || '') + '</template>';

function refSpan(id, label, detail, title) {
  const head = id + (title ? ' · ' + title : (label ? ' · ' + label : ''));
  return '<span class="ref" role="button" tabindex="0" aria-expanded="false">' + esc(label || '对应要求')
    + '<span class="sr">（' + esc(id) + '）</span>' + tipBox(head, tipContent(detail)) + '</span>';
}

/* 正文标注：编号 → 可读名称 + 浮层 */
function annotate(plain, ctx) {
  const keep = [];
  let text = String(plain == null ? '' : plain)
    .replace(/<span class="owner">[\s\S]*?<\/span>|<[^>]+>/g, (mm) => { keep.push(mm); return PUA0 + (keep.length - 1) + PUA1; });
  const restore = (t) => t.replace(new RegExp(PUA0 + '(\\d+)' + PUA1, 'g'), (mm, i) => keep[+i]);
  const ids = Object.keys(PRD_IDS).concat(Object.keys(ctx || {}))
    .filter((v, k, arr) => arr.indexOf(v) === k && !/^§/.test(v)).sort((x, y) => y.length - x.length);
  const secs = Object.keys(PRD_SEC).concat(Object.keys(ctx || {}).filter((k) => /^§/.test(k)).map((k) => k.slice(1)))
    .filter((v, k, arr) => arr.indexOf(v) === k && v.includes('.')).sort((x, y) => y.length - x.length);
  const idAlt = ids.length ? ids.map(escRe).join('|') : '(?!x)x';
  const secAlt = secs.length ? secs.map(escRe).join('|') : '(?!x)x';
  const rx = new RegExp('\\[\\[([^\\]|]+)\\|([^\\]]+)\\]\\]|([A-Z]{2,4}-\\d{3}\\s*[—–~-]\\s*[A-Z]{2,4}-\\d{3})|(' + idAlt + ')|(§?(' + secAlt + '))(?![\\d.])', 'g');
  let out = '', last = 0, m;
  while ((m = rx.exec(text))) {
    out += esc(text.slice(last, m.index)).replace(/ $/, '');
    if (m[1]) {                                     /* 手工指定：[[ID|显示文案]] */
      out += refSpan(m[1], m[2], detailOf(m[1]), titleOf(m[1]));
      last = m.index + m[0].length; rx.lastIndex = last; continue;
    }
    if (m[3]) {                                     /* 区间：FR-001—FR-004 */
      const parts = m[3].split(/\s*[—–~-]\s*/);
      const lab = rangeLabel(parts[0], parts[1]) || (CAT[parts[0].slice(0, 2)] || 'PRD 条目');
      out += refSpan(m[3], lab, detailOf(parts[0]), titleOf(parts[0]));
      last = m.index + m[3].length; rx.lastIndex = last; continue;
    }
    const id = m[4] ? m[4] : ((m[5] && m[5].charAt(0) === '§') ? m[5] : ('§' + m[6]));
    const idLen = m[4] ? m[4].length : (m[5] ? m[5].length : (m[6] ? m[6].length : 0));
    const rest = text.slice(m.index + idLen);
    const lead = rest.match(/^[\s、，,]{0,2}/)[0];
    const after = rest.slice(lead.length);
    const own = (ctx && ctx[id]) || '';
    const prod = PRODNAME(detailOf(id));
    let consumed = 0, preText = '', postText = '', label = '', done = false;
    for (const c of [own, prod]) {
      if (c && after.indexOf(c) === 0) { label = c; consumed = lead.length + c.length; done = true; break; }
    }
    if (!done) {
      for (const c of [own, prod]) {
        if (!c) continue;
        const p2 = after.slice(0, 12).indexOf(c);
        if (p2 > 0) { preText = after.slice(0, p2); label = c; consumed = lead.length + p2 + c.length; done = true; break; }
      }
    }
    if (!done) {
      label = labelOf(id, ctx, after.replace(/^[\s、，,]+/, ''));
      const seg = (after.split(/[；。、，,）)（(【】「」]/)[0] || '').trim();
      if (seg.length >= 4 && seg.length <= 14 && /^[\u4e00-\u9fa5—–\-·]+$/.test(seg)) {
        const nseg = seg.replace(/[—–\-·\s]/g, ''), nlab = label.replace(/[—–\-·\s]/g, '');
        if (nlab.includes(nseg)) label = seg;
      }
      let tail = 0;
      for (let n = Math.min(4, label.length, after.length); n >= 2; n -= 1) {
        const pre = after.slice(0, n);
        if (/^[\u4e00-\u9fa5]+$/.test(pre) && label.slice(-n) === pre) { tail = n; break; }
      }
      if (tail) consumed = lead.length + tail;
      else { consumed = lead.length; preText = ''; postText = (/^\s+$/.test(lead) && /[\u4e00-\u9fa5]/.test(after.charAt(0))) ? '' : lead; }
    }
    out += esc(preText) + refSpan(id, label, detailOf(id), titleOf(id)) + esc(postText);
    last = m.index + idLen + consumed;
    rx.lastIndex = last;
  }
  out += esc(text.slice(last));
  return restore(out);
}
function PRODNAME(d) { const m = /^[“"]?([^”："]{2,12})[：:]/.exec(String(d || '')); return m ? m[1] : ''; }

/* 出处行的标签：编号 → 可读文案 */
function srcChip(seg) {
  let sec = '', id = '', desc = '';
  const m = /^§?(\d+(?:\.\d+)*)\s*(.*)$/.exec(seg);
  if (m && m[2].indexOf('.') < 0) { sec = m[1]; desc = m[2]; } else desc = seg;
  const mm = /^([A-Z]{1,4}-\d{3})\s*(.*)$/.exec(desc);
  if (mm) { id = mm[1]; desc = mm[2]; }
  if (!sec && !id) return null;
  if (sec && sec.indexOf('.') < 0 && !/^§/.test(seg)) return null;
  const dRaw = String(desc || '');
  if (/^(组|条|项|处|个)(前置条件|条件|条|项|处|个)?$/.test(dRaw.trim())) return null;
  if (/^[“"]/.test(dRaw) && !/[”"]$/.test(dRaw.trim())) return null;
  desc = dRaw.replace(/^[“"]/, '').replace(/[”"]$/, '').replace(/^[（(]/, '').replace(/[）)]$/, '').trim();
  const cat = id ? (CAT[id.split('-')[0]] || 'PRD 原文') : '';
  let label = '';
  if (id) {
    if (id.slice(0, 2) === 'AC') label = '验收条件' + (usableDesc(desc) ? ('（' + usableDesc(desc).slice(0, 10) + '）') : '');
    else if (id.split('-')[0] === 'D') label = '产品决策' + (usableDesc(desc) ? ('（' + usableDesc(desc).slice(0, 10) + '）') : '');
    else label = (PRD_IDS[id] && !PRD_IDS[id].includes('、')) ? PRD_IDS[id] : cat;
    if (label.length > 16) label = cat;
  } else {
    const d = usableDesc(desc);
    if (d) label = d;
    else {
      const t = PRD_SEC[sec] || '';
      label = (t && t.length <= 14 && GENERIC_LEAF.indexOf(t) < 0) ? t : 'PRD 原文';
    }
  }
  const hint = (sec ? '§' + sec : '') + (id ? (sec ? ' ' : '') + id : '');
  return { label, hint, raw: seg, detail: detailOf(id || ('§' + sec)) };
}
const chipHtml = (c) => '<span class="ref ref-src" role="button" tabindex="0" aria-expanded="false">'
  + esc(c.label) + '<span class="sr">（' + esc(c.hint) + '）</span>' + tipBox(c.hint, tipContent(c.detail)) + '</span>';

function prdSrcChips(依据) {
  const t = txt(依据).replace(/^PRD\s*/, '');
  const segs = t.split(/、/).map((x) => x.trim()).filter(Boolean);
  const seen = {}, out = [];
  segs.forEach((seg) => { const c = srcChip(seg); if (!c) return; if (seen[c.label]) return; seen[c.label] = 1; out.push(c); });
  if (!out.length) return '';
  const CAP = 4, head = out.slice(0, CAP), rest = out.slice(CAP);
  let html = head.map(chipHtml).join('、');
  if (rest.length) {
    html += '、<span class="ref ref-src" role="button" tabindex="0" aria-expanded="false">等 ' + out.length + ' 处'
      + tipBox('其余 ' + rest.length + ' 处定位', tipLines(rest.map((c) => c.hint + ' · ' + c.label))) + '</span>';
  }
  return 'PRD ' + html;
}
/* 映射表「需求」列：默认只留可读描述，编号进浮层 */
function mapReqCell(需求) {
  const t = txt(需求);
  const ids = [...new Set([...t.matchAll(/[A-Z]{2,4}-\d{3}/g)].map((x) => x[0]))];
  const secs = [...new Set([...t.matchAll(/(?<![\w.])\d+\.\d+(?:\.\d+)?(?![\d.])/g)].map((x) => x[0]))];
  let desc = t.replace(/[A-Z]{2,4}-\d{3}/g, '').replace(/^[\s/、，,·]+/, '').replace(/[\s/、，,·]+$/, '')
    .replace(/\s*\/\s*/g, '／').replace(/\s{2,}/g, ' ').trim();
  desc = desc.replace(/^(?:(?:\d+\.\d+(?:\.\d+)?)\s*[／/、,·]\s*)*(?:\d+\.\d+(?:\.\d+)?)\s*/, '').trim();
  const hints = ids.map((id) => (titleOf(id) ? (id + ' · ' + titleOf(id)) : id))
    .concat(secs.map((x) => '§' + x + (PRD_SEC[x] ? (' · ' + PRD_SEC[x]) : '')));
  if (!desc) desc = hints.length ? 'PRD 条目' : '';
  if (!hints.length) return esc(desc);
  const short = hints.map((h) => h.split(' ')[0]).join('、');
  return '<span class="ref ref-src" role="button" tabindex="0" aria-expanded="false">' + esc(desc)
    + '<span class="sr">（' + esc(short) + '）</span>' + tipBox(short, tipLines(hints)) + '</span>';
}

/* ═══════════════════ 分点：结论说明 / 结论格 ═══════════════════ */
function topSplit(str, sep) {
  const out = []; let depth = 0, cur = '';
  for (const ch of String(str)) {
    if ('（(「《“'.includes(ch)) depth += 1;
    else if ('）)」》”'.includes(ch)) depth = Math.max(0, depth - 1);
    if (ch === sep && depth === 0) { out.push(cur.trim()); cur = ''; continue; }
    cur += ch;
  }
  if (cur.trim()) out.push(cur.trim());
  return out.filter(Boolean);
}
function toPoints(note, opt) {
  const splitAll = opt && opt.splitAll, mismatchFirst = opt && opt.mismatchFirst;
  let segs = topSplit(String(note).replace(/。\s*$/, ''), '；');
  if (mismatchFirst) {
    segs = segs.slice().sort((x, y) => ((/不一致$/.test(y) ? 1 : 0) - (/不一致$/.test(x) ? 1 : 0)));
  }
  const out = [];
  segs.forEach((seg) => {
    const items = topSplit(seg, '、');
    const m = /(一致|不一致)$/.exec(seg);
    if (m && items.length >= 2) {
      const clean = items.map((x, i) => (i === items.length - 1 ? x.replace(/(一致|不一致)$/, '') : x)).filter(Boolean);
      out.push({ group: m[1], items: clean });
      return;
    }
    if (splitAll && items.length >= 3 && items.every((x) => x.length >= 5)) {
      out.push({ points: items.map((x) => x + '。') }); return;
    }
    out.push({ points: [seg + (/[。；]$/.test(seg) ? '' : '。')] });
  });
  return out;
}
function renderPoints(list, ctx) {
  return list.map((b) => {
    if (b.group) {
      const lab = (b.group === '不一致' ? '与 PRD 不一致' : '与 PRD 一致') + ' · ' + b.items.length + ' 项';
      return '<span class="vgrp">' + lab + '</span><ul class="uitems">'
        + b.items.map((x) => '<li>' + annotate(x, ctx) + '</li>').join('') + '</ul>';
    }
    return '<ul class="vpoints">' + b.points.map((x) => '<li>' + annotate(x, ctx) + '</li>').join('') + '</ul>';
  }).join('');
}

/* ══════════════════════════ 渲染报告 ══════════════════════════ */
function render(content, template) {
  const F = (k, d) => (content[k] === undefined ? (d || '') : content[k]);
  const issues = (F('问题', []) || []).map((q, i) => Object.assign({}, q, {
    编号: q.编号 || ('A-' + String(i + 1).padStart(3, '0')),
    级别: q.级别 || 'bad',
  }));
  const badCount = issues.filter((q) => q.级别 === 'bad').length;
  const warnCount = issues.filter((q) => q.级别 === 'warn').length;
  const map = F('映射', []) || [];
  const okCount = map.filter((r) => mapLevel(r.结论) === 'ok').length;
  const unver = F('未验证项', []) || [];
  const checklist = F('补齐清单', []) || [];

  /* 结论三格 */
  const V = F('结论', {});
  const cell = (c) => {
    if (!c) return '';
    const tone = TONE[c.调性] || 'pending';
    const body = (c.分组 && c.分组.length)
      ? c.分组.map((g) => '<span class="vgrp">' + esc(g.标题) + ' · ' + g.项.length + ' 项</span><ul class="uitems">'
          + g.项.map((x) => '<li>' + annotate(x, c.上下文 || {}) + '</li>').join('') + '</ul>').join('')
      : ((c.要点 && c.要点.length)
        ? '<ul class="vpoints">' + c.要点.map((x) => '<li>' + annotate(x, c.上下文 || {}) + '</li>').join('') + '</ul>'
        : ((c.说明 && c.说明.length)
          ? renderPoints(toPoints(c.说明.join('；'), { mismatchFirst: true }), c.上下文 || {})
          : (c.文 ? '<p>' + annotate(c.文, c.上下文 || {}) + '</p>' : '')));
    return '    <div class="vcell ' + tone + '"><span class="k">' + esc(c.标题) + '</span><strong>'
      + esc(c.值) + '</strong>' + body + '</div>';
  };
  const verdicts = [V['审核结论'], V['PRD 审核结论'], V['原型审核结论']]
    .filter(Boolean).map(cell).filter(Boolean).join('\n');

  /* 问题 */
  const issueHtml = issues.map((q) => {
    const ctx = ctxNames(q.需求依据 || '');
    const 出处 = prdSrcChips(q.出处 || q.需求依据 || '');
    return `
      <article class="issue ${q.级别}" data-level="${q.级别}">
        <div class="issue-rail"></div>
        <div class="issue-main">
          <div class="issue-top">
            <span class="level ${q.级别}">${LEVEL_NAME[q.级别] || q.级别}</span>
            <span class="issue-id">${esc(q.编号)}</span>
          </div>
          <h3 class="issue-title">${annotate(q.标题, ctx)}</h3>
          <div class="fact"><span class="k">影响</span><p class="lead">${annotate(q.影响, ctx)}</p></div>
          <div class="fact"><span class="k">怎么改</span><p>${annotate(q.怎么改, ctx)}</p></div>
          <div class="fact"><span class="k">完成标准</span><p class="soft">${q.完成标准 ? annotate(q.完成标准, ctx) : '—'}</p></div>

          <details class="evidence">
            <summary>
              <span class="chev"></span>
              <span class="ev-label">PRD 要求 与 原型实际</span>
            </summary>
            <div class="evidence-body">
              <div class="compare">
                <div class="side prd">
                  <span class="k">PRD 要求</span>
                  <p>${annotate(q.PRD要求, ctx)}</p>
                  ${出处 ? `<span class="src">出处 ${出处}</span>` : ''}
                </div>
                <div class="side proto">
                  <span class="k">原型实际</span>
                  <p>${annotate(q.原型实际, ctx)}</p>
                  ${q.原型出处 ? `<span class="src">出处 ${esc(q.原型出处)}</span>` : ''}
                </div>
              </div>
              ${(q.截图 && q.截图.length) ? `<div class="shots">${q.截图.map((s) =>
                `<figure><img alt="${esc(s.说明 || '原型截图')}" src="${s.data}"><figcaption>${esc(s.说明 || '')}</figcaption></figure>`).join('')}</div>` : ''}
              ${q.技术定位 ? `<div class="tech"><span class="k">技术定位</span><span class="v">${esc(q.技术定位)}</span></div>` : ''}
              ${(q.核对说明 && q.核对说明.length) ? `<ul class="ev-note">${q.核对说明.map((x) => `<li>${annotate(x, ctx)}</li>`).join('')}</ul>` : ''}
            </div>
          </details>
        </div>
      </article>`;
  }).join('\n');

  /* 附录 */
  const pointsHtml = (F('结论说明', []) || []).map((p) => {
    if (typeof p === 'string') return '            <li><p>' + annotate(p) + '</p></li>';
    const head = (p.题 || p.文) ? '<p>' + annotate(p.题 || p.文) + '</p>' : '';
    const items = (p.项 && p.项.length)
      ? '<ul>' + p.项.map((x) => '<li>' + annotate(x) + '</li>').join('') + '</ul>' : '';
    return '            <li>' + head + items + '</li>';
  }).join('\n');

  const mapRows = map.map((r) => {
    const lv = mapLevel(r.结论);
    return '              <tr class="' + (lv === 'ok' ? 'is-ok' : '') + '"><td class="req">' + mapReqCell(r.需求)
      + '</td><td class="loc">' + (r.原型位置 || r.取值 || '') + '</td><td><span class="level ' + lv + '">'
      + esc(r.结论) + '</span></td><td>' + annotate(r.说明 || '') + '</td></tr>';
  }).join('\n');

  const objRows = (F('审核对象', []) || []).map((o) =>
    '            <div><dt>' + esc(o.键) + '</dt><dd>' + annotate(o.值) + '</dd></div>').join('\n');

  const fill = {
    '{{需求名称}}': esc(F('需求名称', '原型设计准入审核')),
    '{{副标题}}': esc(F('副标题', '')),
    '{{结论格}}': verdicts,
    '{{汇总条}}': (F('汇总条', []) || []).map((x) => '<span>' + esc(x) + '</span>').join(''),
    '{{问题数}}': String(issues.length),
    '{{阻断数}}': String(badCount),
    '{{需补齐数}}': String(warnCount),
    '{{问题列表}}': issueHtml,
    '{{未验证项}}': unver.map((x) => '      <li>' + annotate(x) + '</li>').join('\n'),
    '{{结论说明}}': pointsHtml,
    '{{映射统计}}': esc(F('映射统计', map.length + ' 项对照 · 默认只列 ' + (map.length - okCount) + ' 项不一致')),
    '{{映射数}}': String(map.length),
    '{{一致数}}': String(okCount),
    '{{映射行}}': mapRows,
    '{{清单数}}': String(checklist.length),
    '{{清单项}}': checklist.map((x) => '        <li>' + annotate(x) + '</li>').join('\n'),
    '{{对象提示}}': esc(F('对象提示', '')),
    '{{对象行}}': objRows,
  };
  let html = template;
  Object.keys(fill).forEach((k) => { html = html.split(k).join(fill[k]); });
  const left = html.match(/\{\{[^}]+\}\}/g);
  if (left) throw new Error('模板里仍有未填充的占位符：' + [...new Set(left)].join('、'));
  return html;
}

/* ══════════════════════════ 入口 ══════════════════════════ */
function readArgs(argv) {
  const a = {};
  for (let i = 0; i < argv.length; i += 1) {
    const t = argv[i];
    if (!t.startsWith('--')) continue;
    const k = t.slice(2);
    a[k] = (argv[i + 1] && !argv[i + 1].startsWith('--')) ? argv[++i] : true;
  }
  return a;
}
function main() {
  const a = readArgs(process.argv.slice(2));
  if (!a.prd) {
    console.error('用法：node build-report.mjs --prd <PRD.md> [--dump-index | --content <content.json> --out <report.html>] [--template <tpl.html>]');
    process.exit(2);
  }
  buildPrdIndex(a.prd);
  if (a['dump-index']) {
    console.log(JSON.stringify({
      编号: Object.fromEntries(Object.entries(PRD_IDS).map(([k, v]) => [k, v])),
      章节: Object.fromEntries(Object.entries(PRD_SEC).map(([k, v]) => ['§' + k, v])),
    }, null, 2));
    return;
  }
  if (!a.content || !a.out) { console.error('缺少 --content 或 --out'); process.exit(2); }
  const content = JSON.parse(fs.readFileSync(a.content, 'utf8'));
  const tplPath = a.template ? path.resolve(a.template) : DEFAULT_TEMPLATE;
  const html = render(content, fs.readFileSync(tplPath, 'utf8'));
  fs.mkdirSync(path.dirname(path.resolve(a.out)), { recursive: true });
  fs.writeFileSync(a.out, html, 'utf8');
  console.error('已生成 ' + a.out + '（' + Buffer.byteLength(html) + ' 字节）');
}
main();
