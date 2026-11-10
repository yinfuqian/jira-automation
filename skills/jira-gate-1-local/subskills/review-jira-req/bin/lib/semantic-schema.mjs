/**
 * 语义评审结果的结构契约。
 *
 * 单独抽出来是为了让「生成端自检」与「报告端渲染」用同一套规则：
 * 以前 report.mjs 校验 reasons、而自检脚本不校验，导致一份语义结果可以通过自检、
 * 却被报告静默跳过。契约只此一份，两边都引它。
 */

/** 语义评审必须覆盖的维度。 */
export const SEMANTIC_DIMENSIONS = [
  { id: 'atomicity', name: '需求原子性' },
  { id: 'clarity', name: '表述清晰度与无歧义' },
  { id: 'verifiability', name: '可验证性' },
  { id: 'scenario-coverage', name: '边界与异常场景覆盖度' },
];

export const SEMANTIC_CONCLUSIONS = ['合格', '需修改', '不合格', '无法判定'];

/** 模型对单个附件的检查状态。 */
export const ATTACHMENT_USAGE_STATUSES = ['used', 'not-used', 'not-inspected'];

/** 证据来源：描述正文，或某个附件（attachment:<id>）。 */
const SOURCE_PATTERN = /^(description|attachment:\S+)$/;

/** 校验一份 semantic/<KEY>.json。返回问题列表，空数组表示合规。 */
export function validateSemantic(payload) {
  const issues = [];
  if (!payload || typeof payload !== 'object') return ['不是对象'];

  if (typeof payload.key !== 'string' || payload.key === '') issues.push('缺少 key');

  // 附件检查记录是可选字段：旧结果没有它也仍然有效。
  if (payload.attachments !== undefined) {
    if (!Array.isArray(payload.attachments)) {
      issues.push('attachments 必须是数组');
    } else {
      payload.attachments.forEach((attachment, index) => {
        const label = `attachments[${index}]`;
        if (!attachment || typeof attachment !== 'object') {
          issues.push(`${label} 不是对象`);
          return;
        }
        if (
          (typeof attachment.id !== 'string' && typeof attachment.id !== 'number') ||
          String(attachment.id).trim() === ''
        ) {
          issues.push(`${label} 缺少 id`);
        }
        if (typeof attachment.filename !== 'string' || attachment.filename.trim() === '') {
          issues.push(`${label} 缺少 filename`);
        }
        if (!ATTACHMENT_USAGE_STATUSES.includes(attachment.status)) {
          issues.push(`${label} 的 status 非法：${attachment.status}`);
        }
      });
    }
  }

  if (!Array.isArray(payload.dimensions)) {
    issues.push('缺少 dimensions 数组');
    return issues;
  }

  const seen = new Set(payload.dimensions.map((d) => d?.id));
  for (const dimension of SEMANTIC_DIMENSIONS) {
    if (!seen.has(dimension.id)) issues.push(`缺少维度 ${dimension.id}`);
  }

  for (const dimension of payload.dimensions) {
    if (!SEMANTIC_CONCLUSIONS.includes(dimension?.conclusion)) {
      issues.push(`维度 ${dimension?.id} 的 conclusion 非法：${dimension?.conclusion}`);
    }
    if (!Array.isArray(dimension?.reasons) || dimension.reasons.length === 0) {
      issues.push(`维度 ${dimension?.id} 缺少 reasons`);
    }
    if (Array.isArray(dimension?.evidence)) {
      dimension.evidence.forEach((item, index) => {
        if (item?.source !== undefined) {
          if (typeof item.source !== 'string' || !SOURCE_PATTERN.test(item.source)) {
            issues.push(
              `维度 ${dimension?.id} 的 evidence[${index}] source 非法：${item.source}`,
            );
          }
        }
      });
    }
    // 除"无法判定"外，结论必须给出引用原文的证据
    const hasEvidence =
      Array.isArray(dimension?.evidence) &&
      dimension.evidence.some((item) => typeof item?.quote === 'string' && item.quote.trim() !== '');
    if (dimension?.conclusion !== '无法判定' && !hasEvidence) {
      issues.push(`维度 ${dimension?.id} 结论为 ${dimension?.conclusion} 但未引用原文证据`);
    }
    if (dimension?.conclusion === '无法判定' && !dimension?.missingInformation) {
      issues.push(`维度 ${dimension?.id} 标为无法判定但未说明缺失的信息`);
    }
  }

  return issues;
}
