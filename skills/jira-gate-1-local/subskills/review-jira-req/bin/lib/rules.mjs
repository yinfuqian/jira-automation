/**
 * 确定性规则引擎：对一份 OpenSpec 结构的草稿逐条套用规则。
 *
 * 设计依据：design.md → Decisions 3 与 5。
 * 规则语义与严重级别对齐 openspec 1.12.0 的
 * `dist/core/validation/{validator,constants}.js` 与
 * `dist/core/parsers/{spec-structure,requirement-text,code-fence}.js`。
 *
 * 本模块不做任何网络访问，也不调用模型：同样输入必须产出字节级一致的输出。
 */

/**
 * 规则表。severity 取值与 openspec 一致（ERROR / WARNING / INFO）。
 * 改动这里等于改动判定语义，需同步核对 openspec 版本（当前 1.12.0）。
 */
export const RULES = [
  {
    id: 'spec-purpose-missing',
    severity: 'ERROR',
    summary: '缺少 `## Purpose` 段落',
    fix: '补一个 `## Purpose` 段落，用一到两句说明这个能力是干什么的。',
  },
  {
    id: 'spec-purpose-too-brief',
    severity: 'WARNING',
    summary: '`## Purpose` 过短（少于 50 字符）',
    fix: '把 Purpose 扩写到 50 字符以上，说明服务的对象与目的。',
  },
  {
    id: 'spec-no-requirements',
    severity: 'ERROR',
    summary: '没有任何 `### Requirement:` 块',
    fix: '至少写一条 `### Requirement: <名称>` 并附上场景。',
  },
  {
    id: 'requirement-header-malformed',
    severity: 'ERROR',
    summary: '疑似需求标题但格式不合法',
    fix: '需求标题必须严格写成 `### Requirement: <名称>`。',
  },
  {
    id: 'requirement-outside-requirements',
    severity: 'ERROR',
    summary: '需求标题出现在 `## Requirements`（或 delta 标题）之外',
    fix: '把该需求移到 `## Requirements` 之下；否则它不会被解析到。',
  },
  {
    id: 'requirement-duplicate-name',
    severity: 'ERROR',
    summary: '需求名称重复',
    fix: '需求名称必须唯一，否则更新其中一条会连带丢弃另一条。',
  },
  {
    id: 'requirement-empty-body',
    severity: 'ERROR',
    summary: '需求正文为空',
    fix: '在标题下一行写需求正文，并用 SHALL 或 MUST 陈述规范性要求。',
  },
  {
    id: 'requirement-no-shall',
    severity: 'WARNING',
    summary: '需求正文不含 `SHALL` 或 `MUST`',
    fix: '把需求改写为规范性陈述，例如"系统 SHALL ..."（RFC 2119 最佳实践）。',
  },
  {
    id: 'requirement-no-scenarios',
    severity: 'WARNING',
    summary: '需求下没有任何场景',
    fix: '补至少一个 `#### Scenario: <名称>`，用 WHEN/THEN 描述可验证的行为。',
  },
  {
    id: 'requirement-too-long',
    severity: 'INFO',
    summary: '需求正文过长（超过 500 字符）',
    fix: '考虑拆分为多条原子需求，每条只承担一个可验证的行为。',
  },
  {
    id: 'scenario-format',
    severity: 'WARNING',
    summary: '场景未使用 `- **WHEN**` / `- **THEN**` 结构',
    fix:
      '把场景改写成标准结构：\n' +
      '#### Scenario: <短名称>\n' +
      '- **WHEN** <条件>\n' +
      '- **THEN** <预期结果>\n' +
      '- **AND** <可选补充>',
  },
  {
    id: 'scenario-empty',
    severity: 'ERROR',
    summary: '场景内容为空',
    fix: '补齐场景的 WHEN/THEN 描述，或删除这个空场景。',
  },
];

export const MAX_REQUIREMENT_TEXT_LENGTH = 500;
export const MIN_PURPOSE_LENGTH = 50;

const REQUIREMENTS_SECTION_HEADER = /^##\s+Requirements\s*$/i;
const DELTA_SECTION_HEADER = /^(##\s+(ADDED|MODIFIED|REMOVED|RENAMED)\s+Requirements\s*$)/i;
const TOP_LEVEL_SECTION_HEADER = /^##\s+/;
const REQUIREMENT_HEADER = /^###\s+Requirement:\s*(.*?)\s*$/i;
/** 看起来像需求标题但不符合规范（`### Requirement` 少了冒号或名称等）。 */
const REQUIREMENT_HEADER_LOOSE = /^###\s+Requirement\b/i;
/** 任意四级标题都算一个场景，与 openspec 的 SCENARIO_HEADER 保持一致。 */
const SCENARIO_HEADER = /^####\s+/;
const HEADER_LINE = /^#{1,6}\s/;
const METADATA_LINE = /^\*\*[^*]+\*\*:/;
const WHEN_LINE = /^-\s+\*\*WHEN\*\*\s*\S/i;
const THEN_LINE = /^-\s+\*\*THEN\*\*\s*\S/i;

/**
 * 按行构建 fenced code block 掩码，与 openspec 的 buildCodeFenceMask 行为一致。
 * 代码块内的 Markdown 结构（标题、需求块、场景）不算真结构。
 */
export function buildCodeFenceMask(lines) {
  const mask = new Array(lines.length).fill(false);
  let active = null;

  for (let i = 0; i < lines.length; i += 1) {
    if (!active) {
      const opening = lines[i].match(/^\s*(`{3,}|~{3,})/);
      if (opening) {
        active = { marker: opening[1][0], length: opening[1].length };
        mask[i] = true;
      }
      continue;
    }
    mask[i] = true;
    const closing = lines[i].match(/^\s*(`{3,}|~{3,})\s*$/);
    if (closing && closing[1][0] === active.marker && closing[1].length >= active.length) {
      active = null;
    }
  }

  return mask;
}

/** 提取需求正文：跳过空行与 `**元数据**:` 行，遇到下一个标题即停。 */
export function extractRequirementBody(bodyLines) {
  const mask = buildCodeFenceMask(bodyLines);
  const captured = [];
  const metadata = [];

  for (let i = 0; i < bodyLines.length; i += 1) {
    if (mask[i]) continue;
    if (HEADER_LINE.test(bodyLines[i])) break;
    const trimmed = bodyLines[i].trim();
    if (trimmed === '') continue;
    if (METADATA_LINE.test(trimmed)) {
      metadata.push(trimmed);
      continue;
    }
    captured.push(trimmed);
  }

  return captured.length > 0 ? captured.join('\n') : metadata.join('\n');
}

/** 整词匹配 SHALL / MUST。 */
export function containsShallOrMust(text) {
  return /\b(SHALL|MUST)\b/.test(String(text));
}

/**
 * 解析一份 OpenSpec 草稿。
 * 返回 { purpose, purposeLine, requirements, looseHeaders, deltaSections }。
 */
export function parseSpec(content) {
  const normalized = String(content ?? '').replace(/\r\n?/g, '\n');
  const lines = normalized.split('\n');
  const mask = buildCodeFenceMask(lines);

  let requirementsStart = -1;
  let requirementsEnd = lines.length;
  const deltaSections = [];

  for (let i = 0; i < lines.length; i += 1) {
    if (mask[i]) continue;
    if (DELTA_SECTION_HEADER.test(lines[i])) {
      deltaSections.push(i);
      if (requirementsStart === -1) {
        requirementsStart = i;
      } else {
        requirementsEnd = Math.min(requirementsEnd, i);
      }
      continue;
    }
    if (REQUIREMENTS_SECTION_HEADER.test(lines[i]) && requirementsStart === -1) {
      requirementsStart = i;
      for (let j = i + 1; j < lines.length; j += 1) {
        if (mask[j]) continue;
        if (TOP_LEVEL_SECTION_HEADER.test(lines[j])) {
          requirementsEnd = j;
          break;
        }
      }
    }
  }

  // Purpose：取 `## Purpose` 到下一个二级标题之间的内容
  let purpose = null;
  let purposeLine = null;
  for (let i = 0; i < lines.length; i += 1) {
    if (mask[i]) continue;
    if (/^##\s+Purpose\s*$/i.test(lines[i])) {
      purposeLine = i + 1;
      const buf = [];
      for (let j = i + 1; j < lines.length; j += 1) {
        if (!mask[j] && TOP_LEVEL_SECTION_HEADER.test(lines[j])) break;
        if (!mask[j]) buf.push(lines[j]);
      }
      purpose = buf.join('\n').trim();
      break;
    }
  }

  const requirements = [];
  const looseHeaders = [];

  for (let i = 0; i < lines.length; i += 1) {
    if (mask[i]) continue;
    const strict = lines[i].match(REQUIREMENT_HEADER);
    const loose = !strict && REQUIREMENT_HEADER_LOOSE.test(lines[i]);

    if (loose) {
      looseHeaders.push({ line: i + 1, header: lines[i].trim() });
      continue;
    }
    if (!strict) continue;

    // 正文：从标题的下一行开始，直到下一个三级及以上标题或文档末尾
    const bodyLines = [];
    for (let j = i + 1; j < lines.length; j += 1) {
      if (!mask[j] && /^#{1,3}\s/.test(lines[j])) break;
      bodyLines.push(lines[j]);
    }

    const insideRequirements =
      requirementsStart !== -1 && i > requirementsStart && i < requirementsEnd;

    requirements.push({
      name: strict[1].trim(),
      line: i + 1,
      identity: `${strict[1].trim()}@${i + 1}`,
      bodyLines,
      body: extractRequirementBody(bodyLines),
      scenarios: parseScenarios(bodyLines),
      insideRequirements,
    });
  }

  return {
    purpose,
    purposeLine,
    requirements,
    looseHeaders,
    hasRequirementsSection: requirementsStart !== -1,
    hasDeltaSection: deltaSections.length > 0,
  };
}

/** 从需求正文里切出场景块。四级（含更深）标题都算场景。 */
export function parseScenarios(bodyLines) {
  const mask = buildCodeFenceMask(bodyLines);
  const scenarios = [];
  let current = null;

  for (let i = 0; i < bodyLines.length; i += 1) {
    if (!mask[i] && SCENARIO_HEADER.test(bodyLines[i])) {
      current = { line: i + 1, title: bodyLines[i].replace(/^####\s+/, '').trim(), lines: [] };
      scenarios.push(current);
      continue;
    }
    if (current) current.lines.push(bodyLines[i]);
  }

  return scenarios;
}

/** 场景是否用了 `- **WHEN**` / `- **THEN**` 结构。 */
export function scenarioHasWhenThen(scenario) {
  const mask = buildCodeFenceMask(scenario.lines);
  let hasWhen = false;
  let hasThen = false;

  for (let i = 0; i < scenario.lines.length; i += 1) {
    if (mask[i]) continue;
    const line = scenario.lines[i].trim();
    if (WHEN_LINE.test(line)) hasWhen = true;
    if (THEN_LINE.test(line)) hasThen = true;
  }

  return { hasWhen, hasThen, ok: hasWhen && hasThen };
}

/** 场景是否完全没有内容。 */
export function scenarioIsEmpty(scenario) {
  const mask = buildCodeFenceMask(scenario.lines);
  for (let i = 0; i < scenario.lines.length; i += 1) {
    if (mask[i]) continue;
    if (scenario.lines[i].trim() !== '') return false;
  }
  return true;
}

const ruleById = new Map(RULES.map((rule) => [rule.id, rule]));

/** 由规则 id 生成一条问题记录。 */
function issue(ruleId, { requirement = null, line = null, evidence = '', detail = null } = {}) {
  const rule = ruleById.get(ruleId);
  if (!rule) throw new Error(`未知规则 id：${ruleId}`);
  return {
    ruleId,
    severity: rule.severity,
    summary: rule.summary,
    fix: rule.fix,
    requirement,
    line,
    evidence: evidence.length > 120 ? `${evidence.slice(0, 117)}...` : evidence,
    detail,
  };
}

/**
 * 对一份草稿文本跑全部规则。
 *
 * @param {string} content 草稿内容
 * @returns {{ requirements: object[], specIssues: object[], allIssues: object[], counts: object, conclusion: string }}
 */
export function checkSpec(content, { label = null } = {}) {
  const parsed = parseSpec(content);
  const specIssues = [];
  const requirementResults = [];

  // --- 规范级规则 ---
  if (parsed.purpose === null) {
    specIssues.push(issue('spec-purpose-missing', { line: parsed.purposeLine }));
  } else if (parsed.purpose.length < MIN_PURPOSE_LENGTH) {
    specIssues.push(
      issue('spec-purpose-too-brief', { evidence: parsed.purpose, detail: `当前 ${parsed.purpose.length} 字符` }),
    );
  }

  if (parsed.requirements.length === 0) {
    specIssues.push(issue('spec-no-requirements'));
  }

  for (const loose of parsed.looseHeaders) {
    specIssues.push(issue('requirement-header-malformed', { line: loose.line, evidence: loose.header }));
  }

  // 需求名称重复
  const firstSeen = new Map();
  for (const requirement of parsed.requirements) {
    if (firstSeen.has(requirement.name)) {
      specIssues.push(
        issue('requirement-duplicate-name', {
          requirement: requirement.name,
          line: requirement.line,
          detail: `首次出现在第 ${firstSeen.get(requirement.name)} 行`,
        }),
      );
    } else {
      firstSeen.set(requirement.name, requirement.line);
    }
  }

  // --- 逐条需求规则 ---
  for (const requirement of parsed.requirements) {
    const issues = [];

    if (!requirement.insideRequirements) {
      issues.push(
        issue('requirement-outside-requirements', {
          requirement: requirement.name,
          line: requirement.line,
        }),
      );
    }

    if (requirement.body.trim() === '') {
      issues.push(
        issue('requirement-empty-body', {
          requirement: requirement.name,
          line: requirement.line,
        }),
      );
    } else if (!containsShallOrMust(requirement.body)) {
      const inHeader = containsShallOrMust(requirement.name);
      issues.push(
        issue('requirement-no-shall', {
          requirement: requirement.name,
          line: requirement.line,
          evidence: requirement.body,
          detail: inHeader
            ? 'SHALL/MUST 只出现在标题里，请移到 `### Requirement:` 的下一行正文中。'
            : null,
        }),
      );
    }

    if (requirement.body.length > MAX_REQUIREMENT_TEXT_LENGTH) {
      issues.push(
        issue('requirement-too-long', {
          requirement: requirement.name,
          line: requirement.line,
          evidence: requirement.body,
          detail: `当前 ${requirement.body.length} 字符，上限 ${MAX_REQUIREMENT_TEXT_LENGTH}`,
        }),
      );
    }

    if (requirement.scenarios.length === 0) {
      issues.push(
        issue('requirement-no-scenarios', {
          requirement: requirement.name,
          line: requirement.line,
        }),
      );
    }

    for (const scenario of requirement.scenarios) {
      if (scenarioIsEmpty(scenario)) {
        issues.push(
          issue('scenario-empty', {
            requirement: requirement.name,
            line: requirement.line + scenario.line,
            evidence: scenario.title,
          }),
        );
        continue;
      }
      const structure = scenarioHasWhenThen(scenario);
      if (!structure.ok) {
        const missing = [!structure.hasWhen ? 'WHEN' : null, !structure.hasThen ? 'THEN' : null]
          .filter(Boolean)
          .join(' 与 ');
        issues.push(
          issue('scenario-format', {
            requirement: requirement.name,
            line: requirement.line + scenario.line,
            evidence: scenario.title,
            detail: `缺少 ${missing}`,
          }),
        );
      }
    }

    requirementResults.push({
      name: requirement.name,
      line: requirement.line,
      scenarioCount: requirement.scenarios.length,
      bodyLength: requirement.body.length,
      issues,
      conclusion: conclude(issues),
    });
  }

  const allIssues = [...specIssues, ...requirementResults.flatMap((r) => r.issues)];
  const counts = countBySeverity(allIssues);

  return {
    label,
    purpose: parsed.purpose,
    requirements: requirementResults,
    specIssues,
    allIssues,
    counts,
    conclusion: conclude(allIssues),
    rulesEvaluated: RULES.length,
    passedRuleCount: RULES.length - new Set(allIssues.map((i) => i.ruleId)).size,
  };
}

export function countBySeverity(issues) {
  return issues.reduce(
    (acc, item) => {
      acc[item.severity] = (acc[item.severity] ?? 0) + 1;
      acc.total += 1;
      return acc;
    },
    { ERROR: 0, WARNING: 0, INFO: 0, total: 0 },
  );
}

/** 总体结论映射（design.md → Decisions 5）。 */
export function conclude(issues) {
  const counts = countBySeverity(issues);
  if (counts.ERROR > 0) return '不合格';
  if (counts.WARNING > 0) return '需修改';
  return '合格';
}
