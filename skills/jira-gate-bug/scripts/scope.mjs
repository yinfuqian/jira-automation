/**
 * scope.mjs —— 把 Jira 产品/模块/版本解析成故障分析所需的仓库与 Git Tag。
 *
 * 解析只依赖技能目录下的 config/scope-mapping.json，不做模糊猜测。
 */
import { readFileSync } from "node:fs";


const DEFAULT_CONFIG_URL = new URL("../config/scope-mapping.json", import.meta.url);


function normalize(value) {
  return String(value || "").trim().toLowerCase();
}


function readConfig(configPath) {
  const target = configPath ? String(configPath) : DEFAULT_CONFIG_URL;
  const raw = JSON.parse(readFileSync(target, "utf8"));
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("scope-mapping.json 必须是 JSON 对象");
  }
  if (!raw.products || typeof raw.products !== "object" || Array.isArray(raw.products)) {
    throw new Error("scope-mapping.json 缺少 products 配置");
  }
  return raw;
}


function productEntries(config) {
  return Object.entries(config.products || {}).map(([key, value]) => ({
    key,
    value: value && typeof value === "object" ? value : {},
  }));
}


export function resolveProductMapping(product, config, configPath) {
  const wanted = normalize(product);
  if (!wanted) throw new Error("缺少产品：请从 Jira「问题发现产品」读取后传入 --product");
  const loaded = config || readConfig(configPath);
  const matches = productEntries(loaded).filter(({ key, value }) => {
    const aliases = Array.isArray(value.aliases) ? value.aliases : [];
    return normalize(key) === wanted || aliases.some((alias) => normalize(alias) === wanted);
  });
  if (matches.length !== 1) {
    const candidates = productEntries(loaded).map(({ key }) => key).join("、");
    throw new Error(`产品未配置或存在多个匹配：${product}；可用产品：${candidates || "（空）"}`);
  }
  return matches[0];
}


function moduleEntries(productMapping) {
  const modules = productMapping.value.modules || {};
  return Object.entries(modules).map(([key, value]) => ({
    key,
    value: value && typeof value === "object" ? value : { repoUrl: String(value || "") },
  }));
}


export function resolveModuleMapping(productMapping, module) {
  const modules = moduleEntries(productMapping);
  if (!modules.length) throw new Error(`产品 ${productMapping.key} 未配置任何模块仓库`);
  const wanted = normalize(module);
  if (!wanted) {
    if (modules.length === 1) return modules[0];
    throw new Error(`缺少模块：产品 ${productMapping.key} 下配置了多个模块，请提供 --module；候选：${modules.map(({ key }) => key).join("、")}`);
  }
  const matches = modules.filter(({ key, value }) => {
    const aliases = Array.isArray(value.aliases) ? value.aliases : [];
    return normalize(key) === wanted || aliases.some((alias) => normalize(alias) === wanted);
  });
  if (matches.length !== 1) {
    throw new Error(`模块未配置或存在多个匹配：${module}；产品 ${productMapping.key} 可用模块：${modules.map(({ key }) => key).join("、")}`);
  }
  const repoUrl = String(matches[0].value.repoUrl || "").trim();
  if (!repoUrl) throw new Error(`模块 ${matches[0].key} 未配置 repoUrl`);
  return { ...matches[0], repoUrl };
}


/**
 * 列出产品下标记为 common 的公共组件候选。
 * 公共组件不出现在 Jira 评论固定块中，但代码分析需要据此识别真实引用并按需联动拉取。
 */
export function listConfiguredCommonModules(product, config, configPath) {
  const productMapping = resolveProductMapping(product, config, configPath);
  return moduleEntries(productMapping)
    .filter(({ value }) => Boolean(value.common))
    .map(({ key, value }) => {
      const repoUrl = String(value.repoUrl || "").trim();
      if (!repoUrl) throw new Error(`公共组件 ${key} 未配置 repoUrl`);
      return {
        moduleId: `common:${key}`,
        moduleName: key,
        branchAddress: repoUrl,
        common: true,
        aliases: Array.isArray(value.aliases) ? value.aliases : [],
        codePatterns: Array.isArray(value.codePatterns) ? value.codePatterns : [],
      };
    });
}


function renderRuleRef(refTemplate, version, match) {
  return String(refTemplate || "").replace(/\{([^}]+)\}/g, (whole, token) => {
    if (token === "version") return version;
    if (match && match.groups && Object.prototype.hasOwnProperty.call(match.groups, token)) {
      return match.groups[token] || "";
    }
    const index = Number(token);
    return Number.isInteger(index) && match && match[index] !== undefined ? match[index] : whole;
  });
}


function resolveVersionMap(mappings, version) {
  for (const mapping of mappings) {
    const versionMap = mapping && mapping.value && mapping.value.versionMap;
    if (!versionMap || typeof versionMap !== "object" || Array.isArray(versionMap)) continue;
    for (const [sourceVersion, gitTag] of Object.entries(versionMap)) {
      if (String(sourceVersion).trim() !== version) continue;
      const target = String(gitTag || "").trim();
      if (!target) throw new Error(`版本映射 ${sourceVersion} 生成了空 Git Tag`);
      return { sourceVersion: version, gitTag: target, rule: `version-map:${sourceVersion}` };
    }
  }
  return null;
}


export function resolveGitTag(productMapping, moduleMapping, version, config, configPath) {
  const sourceVersion = String(version || "").trim();
  if (!sourceVersion) throw new Error("缺少版本：请从 Jira 版本字段、任务输入或评论固定块读取后传入 --version");
  const loaded = config || readConfig(configPath);
  const mapped = resolveVersionMap([moduleMapping, productMapping, { value: loaded }], sourceVersion);
  if (mapped) return mapped;
  const rules = [
    ...((productMapping && productMapping.value.versionRules) || []),
    ...((moduleMapping && moduleMapping.value.versionRules) || []),
    ...((loaded.versionRules) || []),
  ];
  for (const rule of rules) {
    const pattern = String((rule && rule.pattern) || "");
    if (!pattern) continue;
    const match = new RegExp(pattern).exec(sourceVersion);
    if (!match) continue;
    const ref = renderRuleRef(rule.ref, sourceVersion, match);
    if (!ref) throw new Error(`版本规则 ${rule.name || pattern} 生成了空 Git Tag`);
    return { sourceVersion, gitTag: ref, rule: rule.name || pattern };
  }
  throw new Error(`版本没有匹配任何 Git Tag 规则：${sourceVersion}；请在 scope-mapping.json 的 versionRules 增加规则`);
}


export function resolveConfiguredScope(input = {}, options = {}) {
  const config = options.config || readConfig(options.configPath);
  const product = resolveProductMapping(input.product, config, options.configPath);
  const module = resolveModuleMapping(product, input.module);
  const ref = resolveGitTag(product, module, input.version, config, options.configPath);
  return {
    product: product.key,
    module: module.key,
    common: Boolean(module.value.common),
    repoUrl: module.repoUrl,
    version: ref.sourceVersion,
    gitTag: ref.gitTag,
    rule: ref.rule,
  };
}


export { DEFAULT_CONFIG_URL, readConfig as loadScopeConfig };
