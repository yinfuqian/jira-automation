/**
 * JIRA Server/DC 描述字段的 wiki markup 轻量清理。
 *
 * 目的：把服务端返回的 wiki markup 压成"可见文字 + 换行"，
 * 供后续归一化步骤（LLM）读取。只做去标记，不做语义改写。
 *
 * 依据 design.md → Decisions 4：剥掉表格分隔符、{code}/{noformat} 围栏、
 * h1.–h6. 标题前缀、加粗与斜体标记，保留可见文字与换行。
 *
 * 重要：调用方必须同时保留原始 description（落盘到 raw/），
 * 因为清理是有损的，报告中引用原文时应以原始文本为准。
 */

/** `{code}` / `{code:lang}` / `{noformat}` / `{panel}` 等块级宏的成对围栏。 */
const BLOCK_MACRO = /^\{(code|noformat|panel|quote|info|note|warning|tip|expand|color)(?::[^}]*)?\}\s*$/;

/**
 * 清理 wiki markup，返回适合阅读与 LLM 输入的纯文本。
 */
export function cleanWikiMarkup(input) {
  if (input === undefined || input === null) return '';

  const lines = String(input).replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let inMacro = false;

  for (const rawLine of lines) {
    const line = rawLine;

    // 块级宏围栏：进入/退出时不输出围栏本身，内部内容原样保留
    if (BLOCK_MACRO.test(line.trim())) {
      inMacro = !inMacro;
      continue;
    }

    if (inMacro) {
      out.push(line);
      continue;
    }

    // 表格分隔行（只含 | - : 与空白）：内容无意义，丢弃。
    // 必须放在表格行判断之前，否则会被当成一行普通单元格渲染出来。
    if (/^\s*\|[\s|:-]*\|\s*$/.test(line)) {
      continue;
    }

    // 表格行：||a||b|| 表头与 |a|b| 数据行统一为 "a | b"
    if (/^\s*\|.*\|\s*$/.test(line)) {
      const cells = line
        .trim()
        .replace(/\|\|/g, '|')
        .replace(/^\|/, '')
        .replace(/\|$/, '')
        .split('|')
        .map((cell) => cell.trim())
        .filter((cell) => cell !== '');
      out.push(cells.join(' | '));
      continue;
    }

    out.push(stripInlineMarkup(line));
  }

  return normalizeBlankLines(out).join('\n');
}

/**
 * 标记定界符允许的前后边界。
 *
 * 中文正文里 `*加粗*` 两侧通常没有空格（如"作为*管理员*，"），
 * 所以边界不能只认 ASCII 空白，还必须认中日韩表意文字与全角标点。
 * 常见的算术写法如 `5*3` 因为没有成对定界符，不会被误剥离。
 */
const BOUNDARY_BEFORE = String.raw`(^|[\s(\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])`;
const BOUNDARY_AFTER = String.raw`(?=[\s).,;:!?]|$|[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])`;

/** 处理行内标记与标题前缀。 */
function stripInlineMarkup(line) {
  let text = line;

  // 标题前缀 h1. ~ h6. → 保留文字，丢弃前缀（结构由后续归一化重建）
  text = text.replace(/^\s*h[1-6]\.\s*/, '');

  // 行内代码 {{...}} → 保留内容
  text = text.replace(/\{\{([^}]*)\}\}/g, '$1');

  // 引用前缀 bq.
  text = text.replace(/^\s*bq\.\s*/, '');

  // 加粗 *text* / 斜体 _text_ / 下划线 +text+
  for (const delimiter of ['*', '_', '+']) {
    const pattern = new RegExp(
      `${BOUNDARY_BEFORE}\\${delimiter}([^${delimiter}\\n]+)\\${delimiter}${BOUNDARY_AFTER}`,
      'g',
    );
    text = text.replace(pattern, '$1$2');
  }

  return text.replace(/\s+$/, '');
}

/** 折叠 3 个以上连续空行为 1 个空行，并去掉首尾空行。 */
function normalizeBlankLines(lines) {
  const collapsed = [];
  let blankRun = 0;
  for (const line of lines) {
    if (line.trim() === '') {
      blankRun += 1;
      if (blankRun > 1) continue;
      collapsed.push('');
    } else {
      blankRun = 0;
      collapsed.push(line);
    }
  }
  while (collapsed.length > 0 && collapsed[0] === '') collapsed.shift();
  while (collapsed.length > 0 && collapsed[collapsed.length - 1] === '') collapsed.pop();
  return collapsed;
}

// 自检：`node scripts/lib/wikimarkup.mjs --selftest`
if (process.argv[1] && process.argv[1].endsWith('wikimarkup.mjs') && process.argv.includes('--selftest')) {
  const sample = [
    'h3. 需求描述',
    '',
    '作为*管理员*，我希望_导出报表_，以便存档。',
    '',
    '|| 字段 || 必填 ||',
    '| 名称 | 是 |',
    '| 备注 | 否 |',
    '',
    '{code:java}',
    '// #### Scenario: 这不是真场景',
    'int x = 5*3;',
    '{code}',
    '',
    '详情见 {{JIRA-1}}。',
    '',
    '',
    'bq. 引用一段话',
  ].join('\n');

  const cleaned = cleanWikiMarkup(sample);
  console.log(cleaned);
  console.log('---');
  const checks = [
    ['标题前缀已剥离', !/^h3\./m.test(cleaned)],
    ['加粗标记已剥离', cleaned.includes('作为管理员')],
    ['斜体标记已剥离', cleaned.includes('我希望导出报表')],
    ['表格已经竖线展开', cleaned.includes('字段 | 必填')],
    ['表格分隔行已丢弃', !/\|\s*:-/.test(cleaned)],
    ['code 围栏内容保留', cleaned.includes('int x = 5*3;')],
    ['code 围栏标记已移除', !cleaned.includes('{code')],
    ['行内代码内容保留', cleaned.includes('详情见 JIRA-1。')],
    ['连续空行已折叠', !/\n{3}/.test(cleaned)],
    ['引用前缀已剥离', cleaned.includes('引用一段话')],
    ['算术乘号未被误剥', cleaned.includes('int x = 5*3;')],
  ];
  let failed = 0;
  for (const [label, ok] of checks) {
    console.log(`${ok ? 'PASS' : 'FAIL'} ${label}`);
    if (!ok) failed += 1;
  }
  process.exitCode = failed === 0 ? 0 : 1;
}
