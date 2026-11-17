# 架构说明

`dimcheck` 是一个单进程、单次扫描的离线批处理工具。它没有后台服务、任务队列或外部状态；一次运行从契约与本地样例出发，顺序完成结构校验、数据装载、五类检查、确定性渲染和 SQLite 追加。

## 流水线

```text
CLI 参数
  └─ 契约路径守卫
      └─ YAML 加载 / 结构校验 / 规范指纹
          └─ CSV/JSONL 路径守卫 / null 归一 / 行数上限 / 类型画像
              └─ 声明列与声明类型检查
                  ├─ keys      粒度与代理键/自然键
                  ├─ refs      事实→维引用、雪花维、环
                  ├─ fanout    join 放大估算与实测
                  ├─ measures  可加性、比率、聚合安全矩阵
                  └─ conform   conformed 组、角色、总线矩阵
                      └─ 排序与分级
                          ├─ report.json / report.md / report.html
                          ├─ run.json
                          └─ ledger.sqlite3（追加一次运行）
```

阻断性错误发生在契约或数据装载阶段时，不生成半成品报告：CLI 输出 `DIM-CONTRACT-*`、`DIM-PATH-*`、`DIM-SRC-*` 或 `DIM-LIM-001`，退出码为 `1`。检查阶段发现的问题进入统一发现项列表，按严重级决定退出码 `2` / `3` / `0`。

## 发布协议

文件系统与 SQLite 无法组成同一原生事务，因此一次完成的稽核以**不可变发布单元**落盘，再通过可恢复的幂等步骤与台账对齐：

```text
out/
  publications/pub-<runs.pk>-<report_digest 前12位>/   # 不可变发布单元
      report.json / report.md / report.html / run.json
      manifest.json                                    # 四份文件逐文件 SHA-256
  current -> publications/pub-...                      # 原子切换的最新指针
  report.json|report.md|report.html|run.json -> current/<同名文件>
  quarantine/                                          # 隔离的未完成/损坏单元
  .dimcheck.lock                                       # 目录级 flock 串行化
  ledger.sqlite3
```

- `publication_id = pub-<台账自增主键>-<内容摘要前12位>`。内容摘要只对三份确定性报告正文（JSON/Markdown/HTML）计算，`run.json` 的计时字段是确定性豁免项，不参与摘要。因此样例数据变化但契约指纹不变时，两次稽核仍有不同的 `publication_id` 与摘要。
- 发布分四步，每步可独立重放：① 暂存目录写四份文件并按 manifest 重算实际摘要；② 台账事务写入 `runs/findings/metric_snapshots/publications`（先 `pending`）；③ 单元改名为最终 `publication_id`、补写带 id 的 `run.json` 后置 `committed`；④ 单次原子符号链接切换 `current`。读者通过固定的顶层链接经 `current` 间接寻址，任一时刻只能看到旧的完整单元或新的完整单元。
- 运行时间与 `publication_id` 只出现在 `run.json`、台账与发布清单中，绝不进入 `report.json/report.md/report.html` 正文，报告正文对同一输入保持逐字节稳定。
- 恢复（下次 `run` 或 `serve` 前在同一把锁内执行）始终**重算实际文件摘要**，不相信状态字段。两条都合法的决策：单元完整且 `run.json` 元数据与报告正文交叉核实一致 → 补交台账并成为最新；元数据或摘要无法核实 → 移入 `quarantine/` 并登记 `isolated_publications`，最新停在上一成功运行。重复恢复以 `staging_id`/主键幂等，不会复制台账行，已成功发布的历史不会被回滚或覆盖。
- 两个进程指向同一 `out` 时由 `flock`（同进程内配合可重入 RLock）串行化；台账指针与 `current` 始终指向同一个已提交单元。
- 故障注入：设置 `DIMCHECK_FAULT=file_write|db_commit|promote|view_switch` 在对应步骤抛 `PublicationFault`（映射为 `DIM-IO-001`），`DIMCHECK_FAULT_COUNT` 限定触发次数，供回归测试确定性地模拟四个中断窗口。
- 旧版扁平 `out/report.*` 与旧台账在首次恢复时迁移：旧文件快照为 `pub-legacy-000000` 并登记 `legacy_unverified`，旧 `runs` 行保留可读但**绝不**默认成新协议已验证；`read_runs/read_findings/read_metrics` 签名保持兼容。

## 模块职责

| 模块 | 职责 | 关键边界 |
| --- | --- | --- |
| `cli.py` | 子命令、编排、致命错误到退出码的映射 | 唯一捕获所有异常并输出稳定错误码的边界 |
| `contract.py` | YAML 读取、重复键检测、字段校验、规范化、指纹 | 不做数据读取，不执行表达式 |
| `sources.py` | 路径约束、CSV/JSONL 解析、null 归一、类型推断、行数上限 | 只读；所有源必须在 `dataset.root` 内 |
| `keys.py` | 事实粒度、维度代理键/自然键、键空值/缺失 | 只用规范化值 token 判等，避免类型串扰 |
| `refs.py` | 事实→维成员匹配、未知成员白名单、雪花父子链与环 | 白名单不参与连接匹配，只把孤儿降级 |
| `fanout.py` | 维键重复度、引用重复键、估算/实测放大率、风险分级 | 按单个事实—维引用独立计算 |
| `measures.py` | 可加性/聚合方式冲突、半可加轴、比率定义、数值类型、空值、安全矩阵 | 不重写查询，不将半可加度量的部分和当作全局和 |
| `conform.py` | conformed 组属性/类型/取值域、角色规则、总线矩阵覆盖 | 总线矩阵包含雪花父链的闭包 |
| `report.py` | 报告对象、Markdown/HTML 渲染、四个文件的纯函数式负载渲染 | HTML 内联 CSS，无脚本与外部资源 |
| `pubprotocol.py` | 发布布局常量、内容摘要、逐文件校验、故障注入、目录锁 | 只认实际字节摘要，不读状态字段 |
| `pubfinalize.py` | 暂存单元定稿、`run.json`/manifest 补写、崩溃后补建 | 步骤幂等，可被正常发布与恢复共用 |
| `pubview.py` | `current` 原子符号链接、隔离区、旧扁平目录迁移 | 视图切换是单次 rename，读者只见完整单元 |
| `recovery.py` | 中断恢复：补提交或隔离两条合法路线 | 重算摘要、台账去重、不回滚已发布历史 |
| `publishing.py` | 四步发布编排（写文件→台账→定稿→切视图） | 全程持锁，开头先恢复上一轮残留 |
| `ledger.py` / `ledger_read.py` | 建表迁移、追加运行/发布（写）与历史读取（读） | 只有 INSERT；不 DELETE 历史，旧行标 `legacy_unverified` |
| `server.py` | 只读 `http.server` 页面、静态报告与发布清单 | 仅绑定 `127.0.0.1`，写方法返回 405 |
| `util.py` | 错误码表、发现项、规范 JSON、指纹、时间、路径守卫 | 确定性与安全策略的单一来源 |

## 数据模型

规范化后的 `Contract` 包含：

- 数据集名、已解析的根目录、行数上限、报告缺省 epoch、null token；
- 维度：源、格式、代理键、自然键、属性列表、conformed 组、可选雪花父映射；
- 事实：源、格式、粒度列、引用列表、度量列表；
- 整个 YAML 映射的规范 JSON SHA-256 指纹。

`Dataset` 保存每个实体的 `Table`：稳定列序、归一后的行、逐列画像（类型、空值数、基数、最小/最大、最多三个样例）。列画像用于声明类型核验、可加性安全判断、conformed 取值域与报告展示。

比较值与键时统一使用 `value_token`：`(类型名, 值)` 的规范 JSON。这样整数 `1`、字符串 `"1"`、布尔 `true` 不会互相碰撞，null 也有独立 token。

## 检查顺序与依赖

检查按 `keys → refs → fanout → measures → conform` 运行。它们都只读取同一份不可变的契约和数据集，不互相修改结果。顺序固定是为了让指标字典、台账快照和报告渲染稳定。

- `fanout` 不依赖 `keys` 的发现项；即使代理键未声明为唯一，也会独立计算连接放大。
- `measures` 的比率检查先查找同事实的分子、分母，再检查分母零值和分量可加性。
- `conform` 的角色规则跨事实聚合；conformed 组以字典序最小的成员作为基线。
- 总线矩阵先从事实直接引用的维度出发，再沿雪花 `parent` 关系迭代闭包。

## 确定性策略

1. 输入只来自契约和源文件，不读取当前系统时间生成报告正文。
2. `SOURCE_DATE_EPOCH` 优先；无效或未设置时使用契约的 `run_epoch`。
3. 报告 JSON 使用 `sort_keys=True` 和固定缩进；列表按固定键排序。
4. 发现项按 `(code, object_name, canonical_json(evidence))` 排序；严重级只参与汇总和退出码。
5. 重复键样例、孤儿样例、conformed 差异、总线矩阵、聚合矩阵全部按键排序。
6. 路径、随机 fixture 生成与数据库插入顺序固定。
7. `run.json` 的 `started_at` / `duration_ms` 是明确豁免项；`publication_id`、`report_digest` 同样只允许出现在 `run.json`、台账与发布清单中，台账中的计时列也允许变化。
8. 指纹基于 YAML 解析后的规范值，而非原始文本空白或注释。

## 退出码

| 结果 | 退出码 |
| --- | --- |
| 检查完成，无 error/warning | `0` |
| 检查完成，有 error | `2` |
| 检查完成，仅有 warning | `3` |
| 参数、契约、路径、源、行数上限、输出 IO 失败 | `1` |

“未完成稽核”优先于“完成后的发现级结论”：例如加载数据时行数超限直接 `1`，不会生成带发现项的报告。

## 台账

`ledger.sqlite3` 五张表（schema v2，旧库自动迁移且旧行标记为 `legacy_unverified`）：

- `runs`：运行主键、`run_id`、工具版本、契约指纹、起止元数据、退出码、结论、计数、报告 epoch，以及 `publication_id`、`report_digest`、`publication_status`；
- `findings`：所属运行、序号、错误码、严重级、对象、消息、规范 JSON 证据；
- `metric_snapshots`：所属运行、指标名、规范 JSON 值；
- `publications`：每个发布单元的 `run_pk`、`publication_id`、`staging_id`、内容摘要、状态（`pending/committed/isolated`）与单元路径；
- `isolated_publications`：被隔离单元的摘要、原因与隔离路径，含旧协议快照（`legacy_unverified`）。

`runs.pk` 自增并参与 `publication_id`。每次成功发布追加一行运行与一行发布记录；发现项与五个指标快照也各追加。读取默认取最近一次运行。数据库文件位于 `--out`，不会写入用户主目录。

## 复核台

`serve` 在启动前先在目录锁内执行一次 `recover`，再通过 `ThreadingHTTPServer` 暴露：

- `/`：摘要、当前发布单元 id 与实时摘要校验结果、最近台账；
- `/report.html`、`/report.md`、`/report.json`、`/run.json`（经 `current` 符号链接读取）；
- `/ledger.json`：运行、最新发现项、最新指标；
- `/publications.json`：发布清单、隔离记录与当前视图的字节级校验结论。

只绑定 `127.0.0.1`。只实现 GET/HEAD；POST/PUT/PATCH/DELETE 一律 405。响应带严格 CSP、`nosniff`、`no-referrer` 与 `no-store`。这不是多用户应用，没有认证、上传或写接口。

## 测试结构

- `conftest.py` 构造最小项目，并提供 socket DNS 守卫；
- `test_contract`、`test_sources`、`test_keys`、`test_refs`、`test_fanout`、`test_measures`、`test_conform` 覆盖检查语义；
- `test_report`、`test_determinism` 覆盖渲染与逐字节稳定；
- `test_ledger` 覆盖追加与读取；
- `test_publication`、`test_recovery` 覆盖发布单元、四个中断窗口、并发进程、同契约数据变化、摘要损坏、重复恢复、两条恢复路线与旧目录迁移；
- `test_cli` 覆盖退出码、致命错误与 demo；
- `test_server` 覆盖只读路由与安全头；
- `test_limits` 调用行数自检脚本。

## 扩展点

- 新检查器应接收 `(Contract, Dataset)`，返回 `(list[Finding], dict)`；在 `cli.run_once` 的固定顺序中注册，并补报告与测试。
- 新错误码必须加入 `util.CODES`，同时在 `docs/CODES.md` 说明触发条件与修复方式。
- 新字段要先在 `contract.py` 校验并加入 dataclass，再在所有引用处显式处理；不要让未知字段静默通过。
- 新报告格式必须复用同一 `report` 对象和稳定排序；不得引入当前时间、随机数或无序集合输出。
