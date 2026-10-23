/**
 * 阶段一 HTML 与 Jira 评论渲染、结构校验。
 *
 * 模型只产出 gate.spec.json；HTML 由本模块按固定模板生成，保证结构、编号与文案一致。
 */

import { readFile } from 'node:fs/promises';
import path from 'node:path';

import { EXIT, fail } from './env.mjs';

export function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function escapeWiki(value) {
  return String(value ?? '').replace(/\|/g, '\\|').replace(/\r?\n/g, ' ');
}

export function normalizeSpec(spec) {
  if (!spec || typeof spec !== 'object') fail('gate.spec.json 不是对象。', EXIT.VALIDATION);
  const failures = Array.isArray(spec.failures) ? spec.failures : [];
  const normalized = failures.map((item, index) => {
    const type = String(item.type || '').trim();
    if (!['材料冲突', '材料未明确'].includes(type)) {
      fail(`第 ${index + 1} 项的 type 必须是「材料冲突」或「材料未明确」。`, EXIT.VALIDATION);
    }
    const facts = Array.isArray(item.facts) ? item.facts : [];
    for (const fact of facts) {
      if (!fact || !String(fact.quote || '').trim()) {
        fail(`第 ${index + 1} 项存在空的事实原文。`, EXIT.VALIDATION);
      }
    }
    const closing = Array.isArray(item.closingConditions) ? item.closingConditions : [];
    if (closing.length === 0) fail(`第 ${index + 1} 项缺少关闭条件。`, EXIT.VALIDATION);
    return {
      number: index + 1,
      type,
      topic: String(item.topic || '').trim() || `未命名事项 ${index + 1}`,
      facts,
      problem: String(item.problem || '').trim(),
      closingConditions: closing.map((x) => String(x).trim()).filter(Boolean),
      productAction: type === '材料冲突' ? '修订材料' : '补充结论',
    };
  });
  const conclusion = String(spec.conclusion || '').trim();
  if (!['通过', '暂不通过'].includes(conclusion)) {
    fail('gate.spec.json 的 conclusion 必须是「通过」或「暂不通过」。', EXIT.VALIDATION);
  }
  if (conclusion === '通过' && normalized.length > 0) {
    fail('结论为「通过」时 failures 必须为空。', EXIT.VALIDATION);
  }
  if (conclusion === '暂不通过' && normalized.length === 0) {
    fail('结论为「暂不通过」时 failures 不能为空。', EXIT.VALIDATION);
  }
  return {
    ...spec,
    conclusion,
    failures: normalized,
    nonBlockingFacts: Array.isArray(spec.nonBlockingFacts) ? spec.nonBlockingFacts.map(String) : [],
    materials: Array.isArray(spec.materials) ? spec.materials : [],
  };
}

function renderFacts(facts) {
  return facts
    .map((fact) => {
      const source = escapeHtml(fact.source || '原文');
      return `<blockquote><strong>${source}</strong>：「${escapeHtml(fact.quote)}」</blockquote>`;
    })
    .join('\n');
}

function renderFailure(item, prefix, index) {
  const facts = renderFacts(item.facts);
  const problem = item.problem
    ? `<div class="problem"><strong>${item.type === '材料冲突' ? '冲突' : '当前问题'}</strong>：${escapeHtml(item.problem)}</div>`
    : '';
  const closing = item.closingConditions.map((x) => `<li>${escapeHtml(x)}</li>`).join('');
  return `<h3><span class="pill idx">${prefix}${index}</span>　${escapeHtml(item.topic)}</h3>
  <p><strong>事实依据</strong></p>
  ${facts}
  ${problem}
  <div class="gate"><strong>关闭条件</strong><ol>${closing}</ol></div>`;
}

export async function renderGate(specInput, { templatePath, deliverableVersion, analysisTime }) {
  const spec = normalizeSpec(specInput);
  const version = String(deliverableVersion || spec.deliverableVersion || '1.0').replace(/^v/, '');
  const time = analysisTime || spec.analysisTime || '';
  const template = await readFile(templatePath, 'utf8');
  const title = `${spec.jiraKey} ${spec.h1 || '需求准入门禁清单'}（v${version}）`;
  const h1 = `需求准入门禁清单（v${version}）`;
  const sub = `${escapeHtml(spec.jiraKey)} · ${escapeHtml(spec.requirementName)} · 供产品经理核对需求材料`;

  const counts = {
    材料冲突: spec.failures.filter((f) => f.type === '材料冲突').length,
    材料未明确: spec.failures.filter((f) => f.type === '材料未明确').length,
  };
  const verdict =
    spec.conclusion === '通过'
      ? '<span class="pill pass">通过</span>'
      : `<span class="pill stop">暂不通过：${spec.failures.length} 项未关闭</span>`;

  const overviewRows = spec.failures
    .map(
      (f) =>
        `<tr><td>${f.number}</td><td><span class="pill ${f.type === '材料冲突' ? 'block' : 'pending'}">${f.type}</span></td>` +
        `<td>${escapeHtml(f.topic)}</td><td>${escapeHtml(f.problem)}</td>` +
        `<td>${escapeHtml(f.closingConditions.join('；'))}</td><td>${escapeHtml(f.productAction)}</td></tr>`,
    )
    .join('\n');
  const overview = `<table class="overview">
    <thead><tr><th>编号</th><th>类型</th><th>事项</th><th>当前问题</th><th>关闭条件</th><th>产品需要做什么</th></tr></thead>
    <tbody>${overviewRows || '<tr><td colspan="6">无</td></tr>'}</tbody>
  </table>`;

  const conflicts = spec.failures.filter((f) => f.type === '材料冲突');
  const gaps = spec.failures.filter((f) => f.type === '材料未明确');
  const sectionA = `<section><h2>A. 材料冲突（${conflicts.length} 项，需要修订材料）</h2>${
    conflicts.length
      ? conflicts.map((f, i) => renderFailure(f, 'A', i + 1)).join('\n')
      : '<p>本次核读未发现材料之间或材料内部的业务口径冲突。</p>'
  }</section>`;
  const sectionB = `<section><h2>B. 材料未明确（${gaps.length} 项，需要补充结论）</h2>${
    gaps.length
      ? gaps.map((f, i) => renderFailure(f, 'B', i + 1)).join('\n')
      : '<p>本次核读未发现材料未明确项。</p>'
  }</section>`;

  const nonBlocking = spec.nonBlockingFacts.length
    ? `<ul>${spec.nonBlockingFacts.map((x) => `<li>${escapeHtml(x)}</li>`).join('')}</ul>`
    : '<p>无。</p>';

  const review = [
    `<strong>定位</strong>：输入是 ${escapeHtml(spec.prdVersion ? `PRD ${spec.prdVersion}` : 'Jira 最新需求附件')} 及配套材料；输出是「能否进入下游开发设计」的判定。`,
    '<strong>边界</strong>：QA 只列事实、冲突和关闭条件，不替产品决定具体改法；产品自行选择表述，满足条件并复跑通过即可放行。本清单只用于需求材料准入，不评价开发设计、方案设计与测试方案设计。',
    '<strong>方法依据</strong>：需求准入门禁判据（B1–B7 / C1–C6）。',
    '<strong>证据规则</strong>：每条不通过项均引用需求材料原文；无原文依据的推测不作为阻塞项，不计入不通过项数量。',
    `<strong>口径说明</strong>：凡阻断下游设计的问题都计为不通过；${spec.failures.length} 项全部关闭后重新执行阶段一，结论为「通过」才放行；「有条件通过」或仍有未关闭项时，材料不予放行。`,
  ].join('<br>');

  const materialRows = spec.materials
    .map(
      (m, i) =>
        `<tr><td>${i + 1}</td><td>${escapeHtml(m.filename)}</td><td>${escapeHtml(m.type || '')}</td>` +
        `<td>${escapeHtml(m.id || '')}</td><td>${escapeHtml(m.uploadedAt || m.created || '')}</td></tr>`,
    )
    .join('\n');
  const materials = `<table>
    <thead><tr><th style="width:38px">#</th><th>文件</th><th style="width:15%">版本 / 类型</th><th style="width:12%">Jira 附件</th><th style="width:18%">上传时间（+08:00）</th></tr></thead>
    <tbody>${materialRows || '<tr><td colspan="5">无</td></tr>'}</tbody>
  </table>`;

  const footer = `${spec.jiraKey} · 需求准入门禁清单 v${version} · 分析时间：${time}`;

  return template
    .replace('{{TITLE}}', escapeHtml(title))
    .replace('{{H1}}', escapeHtml(h1))
    .replace('{{SUB}}', sub)
    .replace('{{VERDICT}}', verdict)
    .replace('{{OVERVIEW}}', overview)
    .replace('{{SECTION_A}}', sectionA)
    .replace('{{SECTION_B}}', sectionB)
    .replace('{{NONBLOCKING}}', nonBlocking)
    .replace('{{REVIEW}}', review)
    .replace('{{MATERIALS}}', materials)
    .replace('{{FOOTER}}', escapeHtml(footer));
}

export function renderComment(specInput, { filename }) {
  const spec = normalizeSpec(specInput);
  const version = String(spec.deliverableVersion || '1.0').replace(/^v/, '');
  const lines = [`h3. QA-需求准入门禁结果v${version}`, ''];
  if (spec.conclusion === '通过') {
    lines.push('*结论：通过*');
  } else {
    lines.push(`*结论：暂不通过：${spec.failures.length} 项未关闭*`, '');
    lines.push('||编号||类型||事项||当前问题||关闭条件||产品需要做什么||');
    for (const f of spec.failures) {
      lines.push(
        `|${f.number}|${escapeWiki(f.type)}|${escapeWiki(f.topic)}|${escapeWiki(f.problem)}|` +
          `${escapeWiki(f.closingConditions.join('；'))}|${escapeWiki(f.productAction)}|`,
      );
    }
  }
  lines.push('', `完整清单及原文依据见附件：{{${filename}}}`, '');
  const ids = spec.materials.map((m) => m.id).filter(Boolean).join('、');
  lines.push(`材料版本：PRD ${spec.prdVersion || ''}（Jira 附件 ${ids}）`);
  return lines.join('\n');
}

export async function validateGateFile(htmlPath, specInput, { deliverableVersion, filename } = {}) {
  const html = await readFile(htmlPath, 'utf8');
  const spec = normalizeSpec(specInput);
  const version = String(deliverableVersion || spec.deliverableVersion || '1.0').replace(/^v/, '');
  const errors = [];
  if (!html.includes('<meta charset="utf-8">')) errors.push('缺少 UTF-8 声明');
  if (/<script\b/i.test(html)) errors.push('包含脚本');
  if (/\b(?:src|href)\s*=\s*["']https?:/i.test(html)) errors.push('引用外部资源');
  const h1Tags = [...html.matchAll(/<h1>([^<]*)<\/h1>/g)].map((m) => m[1].trim());
  if (h1Tags.length !== 1) errors.push('H1 标题不唯一');
  if (h1Tags[0] !== `需求准入门禁清单（v${version}）`) errors.push('H1 标题与版本不一致');
  const verdictCount = (html.match(/<span class="label">结论<\/span>/g) || []).length;
  if (verdictCount !== 1) errors.push('结论行数量不为 1');
  const numbers = [...html.matchAll(/<tr><td>(\d+)<\/td>/g)].map((m) => Number(m[1]));
  const expected = spec.failures.length;
  const overviewNumbers = numbers.filter((n) => n >= 1 && n <= expected);
  if (spec.conclusion === '暂不通过' && new Set(overviewNumbers).size !== expected) {
    errors.push('不通过项编号不连续或缺失');
  }
  for (const phrase of ['窄读法', '宽读法', '按字面可理解', '可以读出']) {
    if (html.includes(phrase)) errors.push(`包含禁用表述：${phrase}`);
  }
  if (/<span class="pill[^"]*">[^<]*有条件通过/.test(html)) errors.push('结论不得为「有条件通过」');
  if (filename && !path.basename(filename).startsWith(`${spec.jiraKey}-`)) errors.push('文件名未以 Jira Key 开头');
  if (!html.trimEnd().endsWith('</html>')) errors.push('HTML 未正常结束');
  if (spec.analysisTime && !html.includes(`分析时间：${spec.analysisTime}`)) errors.push('分析时间与 spec 不一致或不在最后一行');
  return { ok: errors.length === 0, errors, html };
}
