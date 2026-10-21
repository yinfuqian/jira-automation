/**
 * scope.mjs —— 把 Jira 产品/模块/版本解析成故障分析所需的仓库与 Git Tag。
 *
 * 解析只依赖技能目录下的 config/scope-mapping.json，不做模糊猜测。
 */
import { readFileSync } from "node:fs";


const DEFAULT_CONFIG_URL = new URL("../config/scope-mapping.json", import.meta.url);


export const SCOPE_ERROR_REASONS = Object.freeze({
  INPUT_MISSING: "scope-input-missing",
  MAPPING_MISSING: "scope-mapping-missing",
  MAPPING_INVALID: "scope-mapping-invalid",
  MAPPING_AMBIGUOUS: "scope-mapping-ambiguous",
});


export const DEFAULT_MAPPING_OWNER = Object.freeze({
  name: "yinfuqian1193",
  displayName: "尹甫乾",
  mention: "[~yinfuqian1193]",
});


export class ScopeResolutionError extends Error {
  constructor(message, reason = SCOPE_ERROR_REASONS.MAPPING_MISSING, details = {}) {
    super(message);
    this.name = "ScopeResolutionError";
    this.reason = reason;
    this.code = reason;
    this.details = { ...details };
  }
}


function scopeError(message, reason, details) {
  return new ScopeResolutionError(message, reason, details);
}


export function isMappingConfigurationReason(reason) {
  return [
    SCOPE_ERROR_REASONS.MAPPING_MISSING,
    SCOPE_ERROR_REASONS.MAPPING_INVALID,
    SCOPE_ERROR_REASONS.MAPPING_AMBIGUOUS,
  ].includes(String(reason || ""));
}


export function resolveMappingOwner(configPath) {
  let config = {};
  try {
    config = readConfig(configPath);
  } catch (error) {
    config = {};
  }
  const owner = config && config.mappingOwner && typeof config.mappingOwner === "object"
    ? config.mappingOwner
    : {};
  const name = String(owner.name || owner.username || DEFAULT_MAPPING_OWNER.name).trim();
  const displayName = String(owner.displayName || DEFAULT_MAPPING_OWNER.displayName).trim();
  const mention = String(owner.mention || `[~${name}]`).trim();
  return { name, displayName, mention };
}


function renderWikiValue(value, fallback = "（未识别）") {
  const text = String(value || "").trim() || fallback;
  return text.replace(/[{}]/g, "");
}


export function buildMappingOwnerComment(error, configPath) {
  const owner = resolveMappingOwner(configPath);
  const details = (error && error.details) || {};
  const product = renderWikiValue(details.product);
  const moduleName = renderWikiValue(details.module);
  const version = renderWikiValue(details.version, "（以工单实际字段为准）");
  const missing = Array.isArray(details.missing) ? details.missing.filter(Boolean) : [];
  const reasonText = String((error && error.message) || "分析范围映射缺失").trim();
  const lines = [
    "h3. 模块映射丢失，已跳过检查",
    "",
    "* 结论：工单中的产品、模块或版本已识别，但 {{config/scope-mapping.json}} 缺少对应映射，本次门禁未检查。",
    `* 待处理：${owner.mention} 请补齐映射内容：产品、模块、仓库地址，以及版本转 Tag 规则。`,
    `* 当前识别：产品={{${product}}}；模块={{${moduleName}}}；版本={{${version}}}。`,
  ];
  if (missing.length) {
    for (const item of missing) lines.push(`* 缺口：${renderWikiValue(item)}`);
  } else {
    lines.push(`* 缺口：${reasonText}`);
  }
  lines.push("* 处理方式：无需提单人重复补充模块；映射补齐后重新触发本技能即可自动检查，不要求额外确认。本单会转派给上述映射维护人，不改变工作流状态。");
  return lines.join("\n");
}


export function buildScopeFailurePayload(error, configPath) {
  const reason = String((error && error.reason) || (error && error.code) || "analysis-error");
  const mappingGap = isMappingConfigurationReason(reason);
  const payload = {
    ok: false,
    reason,
    message: String((error && error.message) || error),
    skipped: mappingGap,
  };
  if (error && error.details && typeof error.details === "object") {
    payload.details = error.details;
  }
  if (mappingGap) {
    payload.gateResult = "未检查";
    payload.mappingOwner = resolveMappingOwner(configPath);
    payload.comment = buildMappingOwnerComment(error, configPath);
  }
  return payload;
}


function normalize(value) {
  return String(value || "").trim().toLowerCase();
}


function readConfig(configPath) {
  const target = configPath ? String(configPath) : DEFAULT_CONFIG_URL;
  let raw;
  try {
    raw = JSON.parse(readFileSync(target, "utf8"));
  } catch (error) {
    throw scopeError(
      `scope-mapping.json 无法读取或解析：${(error && error.message) || error}`,
      SCOPE_ERROR_REASONS.MAPPING_INVALID,
    );
  }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw scopeError("scope-mapping.json 必须是 JSON 对象", SCOPE_ERROR_REASONS.MAPPING_INVALID);
  }
  if (!raw.products || typeof raw.products !== "object" || Array.isArray(raw.products)) {
    throw scopeError("scope-mapping.json 缺少 products 配置", SCOPE_ERROR_REASONS.MAPPING_INVALID);
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
  if (!wanted) {
    throw scopeError(
      "缺少产品：请从 Jira「问题发现产品」读取后传入 --product",
      SCOPE_ERROR_REASONS.INPUT_MISSING,
    );
  }
  const loaded = config || readConfig(configPath);
  const matches = productEntries(loaded).filter(({ key, value }) => {
    const aliases = Array.isArray(value.aliases) ? value.aliases : [];
    return normalize(key) === wanted || aliases.some((alias) => normalize(alias) === wanted);
  });
  if (matches.length !== 1) {
    const candidates = productEntries(loaded).map(({ key }) => key).join("、");
    const reason = matches.length > 1
      ? SCOPE_ERROR_REASONS.MAPPING_AMBIGUOUS
      : SCOPE_ERROR_REASONS.MAPPING_MISSING;
    throw scopeError(
      `产品未配置或存在多个匹配：${product}；可用产品：${candidates || "（空）"}`,
      reason,
      { product: String(product || "").trim(), candidates: productEntries(loaded).map(({ key }) => key) },
    );
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
  if (!modules.length) {
    throw scopeError(
      `产品 ${productMapping.key} 未配置任何模块仓库`,
      SCOPE_ERROR_REASONS.MAPPING_MISSING,
      { product: productMapping.key, module: String(module || "").trim() },
    );
  }
  const wanted = normalize(module);
  if (!wanted) {
    if (modules.length === 1) return modules[0];
    throw scopeError(
      `缺少模块：产品 ${productMapping.key} 下配置了多个模块，请提供 --module；候选：${modules.map(({ key }) => key).join("、")}`,
      SCOPE_ERROR_REASONS.INPUT_MISSING,
      { product: productMapping.key, candidates: modules.map(({ key }) => key) },
    );
  }
  const matches = modules.filter(({ key, value }) => {
    const aliases = Array.isArray(value.aliases) ? value.aliases : [];
    return normalize(key) === wanted || aliases.some((alias) => normalize(alias) === wanted);
  });
  if (matches.length !== 1) {
    const reason = matches.length > 1
      ? SCOPE_ERROR_REASONS.MAPPING_AMBIGUOUS
      : SCOPE_ERROR_REASONS.MAPPING_MISSING;
    throw scopeError(
      `模块未配置或存在多个匹配：${module}；产品 ${productMapping.key} 可用模块：${modules.map(({ key }) => key).join("、")}`,
      reason,
      {
        product: productMapping.key,
        module: String(module || "").trim(),
        candidates: modules.map(({ key }) => key),
      },
    );
  }
  const repoUrl = String(matches[0].value.repoUrl || "").trim();
  if (!repoUrl) {
    throw scopeError(
      `模块 ${matches[0].key} 未配置 repoUrl`,
      SCOPE_ERROR_REASONS.MAPPING_MISSING,
      { product: productMapping.key, module: matches[0].key },
    );
  }
  return { ...matches[0], repoUrl };
}


/**
 * 按模块名反查它配置在哪些产品下。
 *
 * Jira 的「产品归属」只能填一个，但一张缺陷单可以列多个来自不同产品的模块
 * （例如产品归属 Aiforce，同时涉及 aiforce2 与 bot-portal）。关联模块不能一律
 * 套用主产品，必须按这里的映射找到它自己的产品；找不到才回落到主产品。
 */
export function resolveModuleOwners(module, config, configPath) {
  const wanted = normalize(module);
  if (!wanted) return [];
  const loaded = config || readConfig(configPath);
  const owners = [];
  for (const productMapping of productEntries(loaded)) {
    const owned = moduleEntries(productMapping).some(({ key, value }) => {
      const aliases = Array.isArray(value.aliases) ? value.aliases : [];
      return normalize(key) === wanted || aliases.some((alias) => normalize(alias) === wanted);
    });
    if (owned) owners.push(productMapping);
  }
  return owners;
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
      if (!repoUrl) {
        throw scopeError(
          `公共组件 ${key} 未配置 repoUrl`,
          SCOPE_ERROR_REASONS.MAPPING_MISSING,
          { product: productMapping.key, module: key, common: true },
        );
      }
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
      if (!target) {
        throw scopeError(
          `版本映射 ${sourceVersion} 生成了空 Git Tag`,
          SCOPE_ERROR_REASONS.MAPPING_INVALID,
          { version: sourceVersion },
        );
      }
      return { sourceVersion: version, gitTag: target, rule: `version-map:${sourceVersion}` };
    }
  }
  return null;
}


export function resolveGitTag(productMapping, moduleMapping, version, config, configPath) {
  const sourceVersion = String(version || "").trim();
  if (!sourceVersion) {
    throw scopeError(
      "缺少版本：请从 Jira 版本字段、任务输入或评论固定块读取后传入 --version",
      SCOPE_ERROR_REASONS.INPUT_MISSING,
    );
  }
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
    if (!ref) {
      throw scopeError(
        `版本规则 ${rule.name || pattern} 生成了空 Git Tag`,
        SCOPE_ERROR_REASONS.MAPPING_INVALID,
        { product: productMapping && productMapping.key, module: moduleMapping && moduleMapping.key, version: sourceVersion },
      );
    }
    return { sourceVersion, gitTag: ref, rule: rule.name || pattern };
  }
  throw scopeError(
    `版本没有匹配任何 Git Tag 规则：${sourceVersion}；请在 scope-mapping.json 的 versionRules 增加规则`,
    SCOPE_ERROR_REASONS.MAPPING_MISSING,
    {
      product: productMapping && productMapping.key,
      module: moduleMapping && moduleMapping.key,
      version: sourceVersion,
    },
  );
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
