#!/usr/bin/env node
/**
 * 本地 mock JIRA Server/DC 服务，用于端到端验证与无凭据试用。
 *
 * 用法：
 *   node mock-jira.mjs [--port 0] [--print-env]
 *
 * 行为对齐 JIRA Server/DC 的真实约定：
 * - 认证：`Authorization: Bearer <PAT>`，缺失或非预期值返回 401
 * - 单条：GET /rest/api/2/issue/<KEY>，不存在返回 404
 * - 搜索：GET /rest/api/2/search?jql=&startAt=&maxResults=，返回 { total, issues }
 * - `description` 是字符串形式的 wiki markup（非 Cloud 的 ADF）
 *
 * 数据集刻意覆盖多种需求质量：完整规范、缺场景、模糊表述、埋了代码块的伪结构等。
 */

import { createServer } from 'node:http';

export const MOCK_PAT = 'mock-pat-do-not-use-in-production';

/** 演示用附件：一份 Markdown 与一张 1x1 PNG（二进制响应）。 */
const ATTACHMENT_MARKDOWN = [
  '# 批量删除验收标准',
  '',
  '- 用户勾选多件商品后点击"批量删除"，系统 SHALL 一次性删除全部选中项',
  '- 删除前 SHALL 弹出确认，说明将删除的商品数量',
  '- 若部分商品已下架，SHALL 跳过该商品并提示用户',
].join('\n');

const ATTACHMENT_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
);

const ATTACHMENT_FILES = new Map([
  [
    '20001',
    {
      filename: 'acceptance.md',
      mimeType: 'text/markdown',
      data: Buffer.from(ATTACHMENT_MARKDOWN, 'utf8'),
    },
  ],
  [
    '20002',
    {
      filename: 'mock.png',
      mimeType: 'image/png',
      data: ATTACHMENT_PNG,
    },
  ],
]);

/** 数据集：模拟一批从 JIRA 拉回来的需求。 */
export const ISSUES = [
  {
    key: 'SHOP-101',
    fields: {
      summary: '购物车支持批量删除选中商品',
      description: [
        'h3. 需求背景',
        '',
        '用户在购物车中经常需要一次删掉多件商品，目前只能逐个删除。',
        '',
        'h3. 验收标准',
        '',
        '* 用户勾选多件商品后，点击"批量删除"，系统 SHALL 一次性删除全部选中项',
        '* 删除前 SHALL 弹出确认，说明将删除的商品数量',
        '* 删除完成后 SHALL 刷新购物车总价与商品数',
        '* 若删除过程中部分商品已下架，SHALL 跳过该商品并提示用户',
        '',
        'h3. 备注',
        '',
        '|| 字段 || 说明 ||',
        '| 入口 | 购物车列表页顶部工具栏 |',
        '| 权限 | 所有登录用户 |',
      ].join('\n'),
      issuetype: { name: 'Story' },
      status: { name: 'Open' },
      labels: ['cart', 'frontend'],
      assignee: { displayName: '李四' },
      attachment: [
        {
          id: '20001',
          filename: 'acceptance.md',
          mimeType: 'text/markdown',
          size: ATTACHMENT_FILES.get('20001').data.length,
          content: '/secure/attachment/20001/acceptance.md',
        },
        {
          id: '20002',
          filename: 'mock.png',
          mimeType: 'image/png',
          size: ATTACHMENT_FILES.get('20002').data.length,
          content: '/secure/attachment/20002/mock.png',
        },
      ],
    },
  },
  {
    key: 'SHOP-102',
    fields: {
      summary: '订单导出',
      description: [
        '用户希望可以把订单导出来。',
        '',
        '导出要快要稳定，支持各种格式，最好什么格式都能导。',
        '',
        '导出的数据要准。',
      ].join('\n'),
      issuetype: { name: 'Story' },
      status: { name: 'In Progress' },
      labels: [],
      assignee: { displayName: '王五' },
    },
  },
  {
    key: 'SHOP-103',
    fields: {
      summary: '优惠券叠加与互斥规则',
      description: [
        'h3. 规则',
        '',
        '1. 同一订单最多使用 3 张优惠券',
        '2. 满减券与折扣券可以叠加',
        '3. 品类券与全场券互斥',
        '',
        'h3. 示例',
        '',
        '{code}',
        '订单金额 200，使用满100减20 + 9折券 => 应付 (200-20)*0.9 = 162',
        '若同时使用了品类券，则上面的计算不成立，需要按互斥规则拒绝',
        '{code}',
        '',
        'h3. 待定',
        '',
        '叠加后的最优组合是否需要系统自动推荐？暂未确定。',
      ].join('\n'),
      issuetype: { name: 'Story' },
      status: { name: 'Open' },
      labels: ['promotion'],
      assignee: { displayName: '赵六' },
    },
  },
  {
    key: 'SHOP-104',
    fields: {
      summary: '导出任务可取消',
      description: [
        '对进行中和已完成的导出任务，都需要支持取消操作。',
      ].join('\n'),
      issuetype: { name: 'Sub-task' },
      status: { name: 'Open' },
      labels: [],
      assignee: { displayName: '张三' },
    },
  },
  {
    key: 'SHOP-105',
    fields: {
      summary: '商品详情页图片懒加载',
      description: [
        'h3. 目标',
        '',
        '商品详情页首屏图片过多导致加载慢，需要改成懒加载。',
        '',
        'h3. 要求',
        '',
        '* 视口内的图片 SHALL 优先加载',
        '* 视口外的图片 SHALL 在滚动接近时才加载',
        '* 加载失败 SHALL 展示占位图，且不阻塞其余图片',
        '',
        'h3. 场景',
        '',
        '* 用户快速滚动时，系统 SHALL 不重复发起同一张图片的请求',
        '* 网络断开时，系统 SHALL 展示占位图并保留重试入口',
      ].join('\n'),
      issuetype: { name: 'Story' },
      status: { name: 'Open' },
      labels: ['performance'],
      assignee: { displayName: '李四' },
    },
  },
];

const PAGE_HARD_CAP = 2; // 故意小于数据量，用于验证分页

/**
 * 启动 mock server。
 * @returns {Promise<{ baseUrl, port, pat, close, requests }>}
 */
export async function startMockJira({ pat = MOCK_PAT, port = 0, log = false } = {}) {
  const requests = [];

  const server = createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const auth = req.headers.authorization ?? null;
    requests.push({
      method: req.method,
      pathname: url.pathname,
      query: Object.fromEntries(url.searchParams),
      authorization: auth,
    });

    const send = (status, body) => {
      res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify(body));
    };

    const sendBinary = (status, body, contentType) => {
      res.writeHead(status, {
        'content-type': contentType ?? 'application/octet-stream',
        'content-length': body.length,
      });
      res.end(body);
    };

    if (log) {
      process.stderr.write(`[mock-jira] ${req.method} ${req.url} auth=${auth ? 'yes' : 'no'}\n`);
    }

    // 认证：任何非 GET 或凭据不符都拒绝
    if (req.method !== 'GET') {
      send(405, { errorMessages: ['mock 只接受 GET（本工具对 JIRA 只读）'] });
      return;
    }
    if (auth !== `Bearer ${pat}`) {
      send(401, { errorMessages: ['Client must be authenticated to access this resource.'] });
      return;
    }

    const attachmentMatch = url.pathname.match(/^\/secure\/attachment\/([^/]+)\//);
    if (attachmentMatch) {
      const attachment = ATTACHMENT_FILES.get(decodeURIComponent(attachmentMatch[1]));
      if (!attachment) {
        send(404, { errorMessages: ['附件不存在'] });
        return;
      }
      sendBinary(200, attachment.data, attachment.mimeType);
      return;
    }

    const issueMatch = url.pathname.match(/^\/rest\/api\/2\/issue\/([^/]+)$/);
    if (issueMatch) {
      const key = decodeURIComponent(issueMatch[1]);
      const issue = ISSUES.find((item) => item.key === key);
      if (!issue) {
        send(404, { errorMessages: [`Issue ${key} does not exist or you do not have permission to see it.`] });
        return;
      }
      send(200, { key: issue.key, self: `${url.origin}/rest/api/2/issue/${issue.key}`, fields: issue.fields });
      return;
    }

    if (url.pathname === '/rest/api/2/search') {
      const startAt = Number(url.searchParams.get('startAt') ?? 0);
      const maxResults = Math.min(
        Number(url.searchParams.get('maxResults') ?? PAGE_HARD_CAP),
        PAGE_HARD_CAP,
      );
      const page = ISSUES.slice(startAt, startAt + maxResults);
      send(200, {
        total: ISSUES.length,
        startAt,
        maxResults,
        issues: page.map((issue) => ({
          key: issue.key,
          self: `${url.origin}/rest/api/2/issue/${issue.key}`,
          fields: issue.fields,
        })),
      });
      return;
    }

    send(404, { errorMessages: [`mock 未实现的路径：${url.pathname}`] });
  });

  await new Promise((resolve) => server.listen(port, '127.0.0.1', resolve));
  const actualPort = server.address().port;

  return {
    baseUrl: `http://127.0.0.1:${actualPort}`,
    port: actualPort,
    pat,
    requests,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

// 独立运行：起服务并保持，方便手工试用
if (process.argv[1] && process.argv[1].endsWith('mock-jira.mjs')) {
  const withEnv = process.argv.includes('--print-env');
  const portArg = process.argv.indexOf('--port');
  const port = portArg === -1 ? 0 : Number(process.argv[portArg + 1]);
  if (!Number.isInteger(port) || port < 0) {
    process.stderr.write(`--port 需要一个非负整数，收到 "${process.argv[portArg + 1]}"\n`);
    process.exit(2);
  }
  const mock = await startMockJira({ port, log: true });
  const lines = [
    `mock JIRA 已启动：${mock.baseUrl}`,
    `  数据：${ISSUES.length} 条（${ISSUES.map((i) => i.key).join(', ')}）`,
    `  PAT ：${mock.pat}`,
  ];
  if (withEnv) {
    lines.push('', '复制以下内容到你的 shell：', `  export JIRA_BASE_URL="${mock.baseUrl}"`, `  export JIRA_PAT="${mock.pat}"`);
  }
  lines.push('', '按 Ctrl+C 结束。');
  process.stdout.write(`${lines.join('\n')}\n`);
}
