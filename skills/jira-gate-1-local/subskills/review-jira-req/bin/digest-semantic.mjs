// 把某一轮的语义评审结果摊平成可读文本，便于据此写整改建议。
//
// 用法：
//   node digest-semantic.mjs <run-id> <KEY> [KEY...]
//   node digest-semantic.mjs <run-id> --all
//
// 只读 reports/.work/<run-id>/semantic/，不联网、不改任何文件。
import { readFileSync, readdirSync } from 'node:fs';

const [runId, ...rest] = process.argv.slice(2);
if (!runId) {
  console.error('用法：node digest-semantic.mjs <run-id> <KEY> [KEY...] | <run-id> --all');
  process.exit(2);
}

const dir = `reports/.work/${runId}/semantic`;
const keys = rest.includes('--all')
  ? readdirSync(dir)
      .filter((f) => f.endsWith('.json'))
      .map((f) => f.replace(/\.json$/, ''))
      .sort()
  : rest;

if (keys.length === 0) {
  console.error('没有指定 Key。用 <KEY>... 或 --all。');
  process.exit(2);
}

for (const k of keys) {
  const s = JSON.parse(readFileSync(`${dir}/${k}.json`, 'utf8'));
  console.log(`\n########## ${k}`);
  for (const d of s.dimensions) {
    console.log(`  [${d.id}] ${d.conclusion}`);
    for (const e of d.evidence ?? []) {
      console.log(`    · quote: ${e.quote.replace(/\n/g, '\\n').slice(0, 90)}`);
      if (e.why) console.log(`      why: ${e.why}`);
      console.log(`      rewrite: ${e.rewrite === null ? '(仅建议补场景)' : e.rewrite}`);
    }
    if (d.missingInformation) console.log(`      missing: ${d.missingInformation}`);
  }
  if (s.missingScenarios?.length) console.log(`  缺失场景类别: ${s.missingScenarios.join('、')}`);
  for (const sc of s.suggestedScenarios ?? []) {
    console.log(`  + 建议场景《${sc.title}》 WHEN ${sc.when} / THEN ${sc.then}`);
  }
  if (s.nonAtomic?.detected) {
    console.log('  拆分建议:');
    for (const x of s.nonAtomic.suggestedSplit ?? []) console.log(`    - ${x}`);
  }
}
