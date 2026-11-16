# 维模体检台（dimcheck）

`dimcheck` 是一个**纯本地、完全离线、结果确定性**的数仓维度模型静态稽核与聚合安全体检工具。它读取声明式模型契约（YAML）和本地 CSV/JSONL 样例，回答一个具体问题：

> 这套星型维度模型，键是否可信、粒度是否成立、join 是否会放大、度量是否可安全聚合？

它是 Coding Agent 评测用的可运行素材源项目：不包含题面、不包含评测用例，只包含工具、示例契约、合成 fixture、文档与回归测试。

## 定位

**是：**

- Kimball 风格星型模型的声明式契约稽核器：契约声明事实粒度、维度代理键/自然键、度量可加性、维度一致性约束，工具逐条核验并给出证据。
- 聚合安全与 fan-out 风险分析器：检查维表代理键不唯一、事实与维表连接后的行数放大、半可加与比率语义被错误求和。
- 一致性维度与角色扮演维度检查器：检查 `conformed_group` 内属性、类型、取值域；检查重复引用是否显式声明角色。
- 可审计的本地批处理：一条命令跑通契约加载、数据画像、键/粒度、引用完整性、放大分析、可加性、一致性和报告台账。
- 合成 AFC 客流主题 fixture：示例城市、线路、站点全部为虚构代号（`示例市`、`L-01`、`S-A01`），不指向任何真实主体。

**不是：**

- 不是 SQL 执行引擎、ETL 工具，不做清洗、修复或写回。
- 不是指标口径校准器，不比对报表 SQL 与口径字典。
- 不是 SCD2 历史回放或 as-of 对账工具。
- 不是 SQL 字段级血缘抽取器。
- 不是数据湖快照、时间旅行、分区裁剪、小文件合并或冷热分层工具。
- 不是 Web 后台：没有用户体系、上传、下载中心或写接口。

## 与相邻主题的边界

| 相邻主题 | 它回答的问题 | 本项目回答的问题 |
| --- | --- | --- |
| 指标口径校准 | 指标算法是否漂移 | 模型结构是否允许安全聚合 |
| 字段血缘 | 字段从哪里来 | 这一列能不能被 `sum` |
| 湖仓维护 | 文件、快照、分区如何整理 | 只读样例数据，不移动、不改写源文件 |

## 快速开始

要求 Python 3.10+；运行期只依赖 `PyYAML`，测试依赖 `pytest`。依赖安装完成后，运行期不需要网络。

```bash
python -m pip install -r requirements.txt
python -m pip install -e .
python -m dimcheck demo --out out
pytest -q
```

如果环境不允许安装，可在仓库根目录用 `PYTHONPATH=src python -m dimcheck demo --out out`；PowerShell 对应 `$env:PYTHONPATH="src"`。`demo` 需要源码检出中的 `contracts/` 与 `scripts/`。

`demo` 会以固定 seed 重建 `fixtures/`，加载 `contracts/model.yaml`，跑完全部检查，写入报告与 SQLite 台账。示例模型应当返回退出码 `0`。

其他命令：

```bash
python -m dimcheck run --contract contracts/model.yaml --out out
python -m dimcheck explain DIM-FAN-001
python -m dimcheck serve --out out --port 8765
python scripts/make_fixtures.py --root fixtures
python scripts/check_limits.py
```

安装包后也可使用控制台入口 `dimcheck`。`serve` 只绑定回环地址 `127.0.0.1`，只提供只读页面和 JSON 视图。

## 产物

一次 `run` / `demo` 在 `--out`（默认 `./out`）生成：

| 文件 | 内容 |
| --- | --- |
| `report.json` | 完整结论、分级发现项、指标快照、数据画像；确定性排序 |
| `report.md` | 可归档 Markdown 报告 |
| `report.html` | 自包含 HTML：内联 CSS，无脚本、外部字体、图标或 CDN |
| `run.json` | 运行元数据；`started_at` 与 `duration_ms` 是唯一允许非确定性的字段 |
| `ledger.sqlite3` | 追加式 SQLite 台账：运行、发现项、指标快照 |

报告文件在重复运行时更新；台账按自增主键追加，不删除历史。`run_id` 由数据集名和契约指纹派生。

## 检查能力

| 阶段 | 关键问题 | 主要错误码 |
| --- | --- | --- |
| 契约 | 字段、类型、引用、命名是否合法 | `DIM-CONTRACT-*` |
| 数据源 | CSV/JSONL 是否可读、列是否一致、声明类型是否匹配 | `DIM-SRC-*`、`DIM-LIM-001` |
| 键与粒度 | 事实粒度唯一？代理键/自然键唯一？键是否为空？ | `DIM-KEY-*` |
| 引用完整性 | 事实→维孤儿、空外键、雪花维父子链与环 | `DIM-REF-*` |
| 放大分析 | 维键重复度、估算/实测 join 倍数、风险分级 | `DIM-FAN-001` |
| 聚合安全 | 可加性声明、半可加轴、比率定义、数值类型、空值 | `DIM-MEA-*` |
| 一致性 | conformed 属性/类型/取值域、角色扮演、总线矩阵覆盖 | `DIM-CON-*` |

发现项分 `error` / `warning` / `info`。每个发现项都带对象名、说明、修复建议和 JSON 证据；证据中的样例按稳定键排序。

## 契约与数据

- 契约字段参考与最小示例见 [`contracts/README.md`](contracts/README.md)。
- 示例 AFC 契约见 [`contracts/model.yaml`](contracts/model.yaml)。
- 数据源根目录由 `dataset.root` 指定，路径相对契约文件所在目录解析；所有 `source` 必须解析到该根目录内部。
- 支持 CSV 与 JSONL；空字符串、`NULL`、`null`（可配置）归一为空值。
- 单次运行的输入总行数默认上限为 `500000`，由 `dataset.max_rows` 控制；超限报 `DIM-LIM-001` 并以退出码 `1` 结束。

## 退出码

| 码 | 含义 |
| --- | --- |
| `0` | 稽核完成，无 error、无 warning（允许 info） |
| `1` | 参数、契约、路径、数据源、行数上限或输出 IO 失败，未能完成稽核 |
| `2` | 稽核完成，且存在至少一个 `error` |
| `3` | 稽核完成，仅有 `warning` |

完整目录见 [`docs/CODES.md`](docs/CODES.md)。

## 确定性与时间

- 除 `run.json` 的 `started_at` / `duration_ms` 外，同一输入、同样 `SOURCE_DATE_EPOCH` 下，`report.json`、`report.md`、`report.html` 逐字节一致。
- 发现项按 `(错误码, 对象名, 规范化证据)` 排序；证据中的列表与映射有稳定顺序。
- 报告时间来自 `SOURCE_DATE_EPOCH`；缺省时使用契约中的 `run_epoch`。报告正文不读取系统当前时间。
- 指纹使用规范 JSON 的 SHA-256；不使用随机数或哈希种子写报告。

## 离线、数据与安全

- 运行期不发起任何网络请求，不进行 DNS 查询，不访问 CDN；HTML 完全自包含。
- 只读取契约指定的数据集根目录内部数据，以及用户显式给出的本地契约路径；路径穿越、UNC 网络路径与系统敏感目录被拒绝。
- `run` 的产物只写入 `--out`；`demo` 额外按固定 seed 重建仓库内 `fixtures/`。工具不在用户主目录隐式创建文件。台账存在时追加，不静默覆盖。
- 仓库内 fixture 与文档不含真实密钥、令牌、私钥、真实公司、真实域名、真实 IP 或真实站点地址；唯一出现的地址字面量为回环地址 `127.0.0.1`。
- 复核台只读，只监听 `127.0.0.1`；没有上传、删除或写接口。

更多说明见 [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md)。

## 本地维表交付登记

`python -m dimcheck.intake fixtures/dimension-delivery.json --root fixtures --ledger <本地台账路径>`
会校验示例维表交付清单、逐份 CSV 的字节摘要和行数，并登记不可变的
`batch_id`、生产方及文件收据。相同清单重试幂等；同一批次身份换生产方或内容
会报冲突。此流程只读来源 CSV，台账写在调用者指定的本地路径；可用
`python -m pytest -q tests/test_intake.py` 复验正常、冲突和坏摘要路径。

## 项目结构

```text
contracts/               契约、字段说明
docs/                    ARCHITECTURE / CODES / COMPLIANCE
fixtures/                demo 生成的合成数据
scripts/                 fixture 生成、行数预算自检
src/dimcheck/            CLI、契约、数据源、检查器、报告、台账、复核台
tests/                   单元、集成、确定性、离线与台账回归
```

模块与流水线细节见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 开发约束

- Python 3.10+；不使用 pandas、pyarrow、pydantic、jinja2、requests。
- Python 代码总行数目标 `2500..4500`；任意单文件不超过 `300` 行。`scripts/check_limits.py` 自检，`pytest -q` 中有回归测试断言该自检通过。
- 运行期依赖仅 `PyYAML`；其余使用标准库。
- 所有测试离线运行，并带 socket DNS 守卫。

## 已知限制

- 只对声明的事实粒度和样例数据做静态/样例核验，不证明全量生产数据成立。
- 半可加度量不会被自动改写；工具只判断声明与轴是否合法，并提示安全聚合路径。
- 不展开 SCD2；自然键重复仅给 warning。
- 放大分析按单个事实—维引用实测；多个引用造成的复合交互由报告中的分项指标呈现。
- 不执行 SQL、不推断查询语义，因此不能替代查询级聚合审计。
