# jxtest 开发纲领（guideline）

## 项目愿景（Mission）

> **做一个开源、AI 原生的 API 测试平台，覆盖 Postman 80% 核心场景，并让 AI 自主驱动测试任务。**

我们不追求 Postman 的所有功能（特别是 UI 团队协作），而是抓住两个核心：
1. **替代 Postman 在个人开发 / 自动化 / CI 场景下的使用**
2. **让 AI 直接驱动测试**（自动生成、自动诊断、自动修复）

## 目标用户（Persona）

1. **AI 工程师 / Prompt 工程师**：用 Claude / GPT 驱动测试，需要工具能被 AI 理解
2. **后端开发者**：厌倦 Postman 鼠标点击流，想要测试代码化
3. **QA 工程师**：需要在 CI 中跑测试，需要 JUnit / Allure 报告
4. **小团队**：买不起 Postman 团队版，想要免费 + git 协作

## 核心目标（Goals）

| 目标 | 度量 |
|------|------|
| 替代 Postman 核心功能 | 8 个 Skills 全部实现 |
| AI 可直接驱动 | 每个 SKILL.md < 100 行，AI 一次读懂 |
| 脚本 < 250 行 | 主流 SKILL.md 配套脚本保持精简 |
| Stdlib 优先 | 仅在必要时引入第三方库 |
| 端到端可跑 | 跑通 `petstore` 示例 |
| 报告美观 | 至少支持 HTML + JUnit XML |

## 范围（Scope）

### ✅ 必须做（v1）

**协议层：**
- HTTP / 1.1（已有）
- HTTPS / TLS
- WebSocket
- gRPC（基础支持）

**认证层：**
- Bearer Token
- Basic Auth
- API Key (Header / Query)
- OAuth 2.0（Client Credentials / Password / Auth Code）

**变量层：**
- 环境变量（dev/staging/prod）
- 三层作用域（global / env / case）
- 模板语法 `{{var}}`
- 动态生成（UUID / timestamp / random）

**测试层：**
- 30+ 断言类型
- 预请求脚本（Python）
- 测试前后钩子
- 集合（Collection）文件夹
- 数据驱动（CSV / JSON）

**AI 层：**
- 自动测试生成（已有）
- 自动失败诊断
- 断言自愈
- 自动 Mock 服务
- 自然语言 → API 测试

**输出层：**
- HTML 报告（已有）
- JSON 报告
- JUnit XML（CI）
- Markdown 文档

**集成层：**
- CLI 工具
- Makefile 脚本

> GitHub Actions 示例、Docker 镜像等外部集成资产计划在 v1.5 (Phase 2) 提供——v1 阶段以本地 `make` 流程为 CI 入口即可。

### ❌ 不做（Non-Goals）

- ❌ **Desktop GUI / Web UI** — 用户用 CLI + AI；Postman 的 UI 是它的核心，但 jxtest 不需要
- ❌ **团队云 / Workspace** — 用 Git 替代；用户能 fork 协作
- ❌ **API 监控 / 定时运行** — 用 CI 替代；Postman Monitor 是付费功能
- ❌ **Mock Server 完整版** — 只做基础 schema-based mock；复杂场景用 WireMock
- ❌ **API 设计（design-first）** — 专注于 testing，不做 API 设计工具
- ❌ **付费版 / 商业版** — 全开源

## Skill 蓝图（v1）

| # | Skill | 状态 | 作用 |
|---|-------|------|------|
| 1 | `api-test-schema` | ✅ | 解析 OpenAPI / Postman / HAR → api-spec.json |
| 2 | `api-test-env` | ✅ | 环境管理 + 变量模板 + 三层作用域 |
| 3 | `api-test-mock` | ✅ | AI 生成 mock server |
| 4 | `api-test-gen` | ✅ | 从 api-spec.json 生成测试用例（含 defaults） |
| 5 | `api-test-run` | ✅ | 执行（env/var/OAuth2/脚本/15+ 断言/JUnit/config） |
| 6 | `api-test-load` | ✅ | **压力测试（VUs/duration/ramp-up/p50-p99）** |
| 7 | `api-test-heal` | ✅ | LLM 分析失败 + 自愈 |
| 8 | `api-test-report` | ✅ | HTML 报告 |
| 9 | `api-test-doc` | ✅ | Markdown API 文档生成 |

## 数据流（Data Flow）

```
[OpenAPI/Postman/HAR]
        │
        ▼
[api-test-schema] ───────────────────┐
        │                            │
        ▼                            │
   api-spec.json                     │
        │                            │
        ├─────────────────┐          │
        ▼                 ▼          │
[api-test-mock]    [api-test-gen]   │
        │                 │          │
        ▼                 ▼          │
  mock server       test-cases.json  │
                          │          │
                          ▼          │
                    [api-test-run] ◄─┤  ← 读 api-spec.json 做响应 schema 校验
                          │          │
                          ▼          │
                    test-results.json
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
        [api-test-heal] [report]  [api-test-doc]
              │           │           │
              ▼           ▼           ▼
         fixed cases  report.html  docs.md
```

## 设计原则（Principles）

1. **AI 可理解（AI-comprehensible）**
   - 每个 Skill 一个 SKILL.md
   - 明确 I/O contract
   - 不藏逻辑，AI 可以阅读源码

2. **精简（Concise）**
   - SKILL.md < 100 行
   - 脚本 < 250 行
   - 拆任务而非堆代码

3. **能用（Functional）**
   - 每个 Skill 都能独立运行
   - 端到端能跑通
   - 失败要明确报错

4. **完善（Complete）**
   - 覆盖 spec → 测试 → 执行 → 报告全链路
   - 提供示例 + 文档
   - 提供 CI 集成示例

5. **Stdlib 优先**
   - 除非必要不用第三方库
   - 必要时在 SKILL.md 顶部声明 dependencies

## 路线图（Roadmap）

### Phase 1: 核心可用（v1.0）— 10-15 个工作日
- [x] api-test-schema
- [x] api-test-env
- [x] api-test-mock
- [x] api-test-gen（含 defaults）
- [x] api-test-run（OAuth2 / 脚本 / 15+ 断言 / JUnit / config）
- [x] api-test-load（VUs / duration / 延迟分位数 / 报告）
- [x] api-test-heal
- [x] api-test-report
- [x] api-test-doc
- [x] 参数封装：jxtest.config.json + defaults
- [x] README + 集成示例

### Phase 2: 增强（v1.5）— 后续
- [ ] WebSocket 协议
- [ ] gRPC 协议
- [ ] GraphQL 协议
- [ ] 数据驱动（CSV / JSON）
- [ ] 集合（Collection）文件夹
- [ ] 历史记录 + 趋势分析
- [ ] k6 导出（极限压力）

### Phase 3: 生态（v2.0）— 远期
- [ ] Claude Code 插件发布
- [ ] VS Code 扩展
- [ ] Web 报告（轻量）
- [ ] 团队协作（git-based）
- [ ] API 监控集成

## 验收标准（Definition of Done）

每个 Skill 完成的标志：
- ✅ SKILL.md 写完
- ✅ 脚本能跑通
- ✅ 有 1 个端到端示例覆盖
- ✅ README 索引包含
- ✅ 通过 `make validate` 或同等检查

整个 v1.0 完成的标志：
- ✅ 跑通 petstore 完整示例（schema → gen → run → report）
- ✅ 至少 1 个 Skill 包含启发式智能（heal 自动安全修复 / mock 智能选择行为）
- ✅ 文档齐全（README + GUIDELINE + 每 Skill SKILL.md）
- ⬜ CI 集成示例（GitHub Actions workflow 待补；当前 `make ci` 可作为本地入口）

## 风险与应对（Risks）

| 风险 | 应对 |
|------|------|
| OAuth2 实现复杂 | 只做 Client Credentials，其他写 TODO |
| WebSocket 异步逻辑 | 用 threading 包装，封装成同步接口 |
| LLM 输出不稳定 | 限制输出格式为 JSON Schema |
| gRPC 协议重 | Phase 2 再说；除非用户明确要 |
| 第三方库依赖难以维护 | Stdlib 优先；依赖明示在 SKILL.md |

## 更新记录

- 2026-08-02: 创建初始版，定义 8 个 Skill 蓝图
- 2026-08-03 (v1.1): 优化 jxtest per experience report — `Optimize jxtest per experience report`
- 2026-08-03 (v1.2): AI-native preflight + safer auto-heal — `Fix P0 crash + harden UX per ERP实战问题汇总`，新增 `doctor` skill，断言自愈走纯启发式
- 2026-08-03 (v1.3): per ERP 实战 v2 体验报告

  **核心 bug 修复**
  - `gen` 的 `_fill_params` 现在跳过 auth header 形参(`Authorization` / `X-API-Key` / `X-Auth-Token`,或 `spec.auth.header` 指定);原本会把 OpenAPI 里的 `parameters[].in=header, name=authorization` 当成 query 写入 case.headers,与 auth block 同时下发造成 124 条废 case。stderr 列出被剔除项。

  **六层防御 + 可视性**
  - `doctor` 新检查 `duplicate_auth_header`(warning):扫 case.headers 与 auth header 同名(大小写不敏感)的非空键
  - `heal` 新 pre-pass:清理 test-cases.json 中冲突的 auth header 键,dry-run 也输出 `headerRemovals`
  - `coverage` 新增 `not_called_due_to_auth` + `effective_coverage_pct`:区分"未调用"和"调用了但全是 401/403"
  - `report` HTML 报告新增 "Failures by category × failure class" 矩阵;boundary 行加 `status_in` 徽章;boundary+4xx 失败详情里加 NOTE
  - `env` `env set` 检测头部形 key 名(`Authorization`/`X-API-Key`/`X-Auth-Token`/`Cookie`)或值以 `Bearer ` 开头,提示改用 `test-cases.json:auth`;`env validate` 也把这类 key 列为 issue
  - `security` 引入 `configFindings[]`(severity=low,与 vulnerabilities 分开):扫 spec 里与 auth header 同名的 `parameters[].in=header`,给 remediation

  **文档**
  - `SKILL.md`(主)+ 6 个分片 SKILL.md 同步更新
  - README Highlights 加 3 条
  - bin/jxtest `--version` 1.2 → 1.3
