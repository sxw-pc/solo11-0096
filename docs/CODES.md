# 错误码目录

共 54 个稳定错误码。`python -m dimcheck explain` 列出全部；`python -m dimcheck explain DIM-FAN-001` 查看单条。错误码是稳定接口，新增码不得改变既有码的含义。

## 严重级与退出码

| 严重级 | 含义 | 对退出码的影响 |
| --- | --- | --- |
| `error` | 阻断；模型或数据不满足安全聚合前提 | 完成稽核后为 `2` |
| `warning` | 风险；需要人工复核或业务确认 | 无 error 时为 `3` |
| `info` | 提示；不阻断 | 不改变退出码 |

契约、路径、数据源加载或行数上限失败时，不进入报告，直接退出 `1`。`DIM-IO-001`、`DIM-CLI-001`、`DIM-INTERNAL-001` 等运行边界错误同样退出 `1`。

`DIM-REF-002` 是唯一动态严重级：默认 `warning`；当引用声明 `optional: false`（缺省值）时按 `error` 处理，当 `optional: true` 时保持 `warning`。

## 契约与路径

| 代码 | 级别 | 触发条件 | 修复建议 | 示例 |
| --- | --- | --- | --- | --- |
| `DIM-CONTRACT-001` | error | YAML 不是映射、字段类型错误、缺少必填字段、枚举值非法、比率缺分子/分母、列表为空等结构问题 | 按字段参考修正类型与必填项 | `dimensions[0].attributes` 不是列表 |
| `DIM-CONTRACT-002` | error | YAML 重复键、维度/事实/属性/度量重名、列表内重复值 | 使用唯一名称，删除重复键 | 两个维度都叫 `dim_customer` |
| `DIM-CONTRACT-003` | error | 事实引用未知维度、雪花父维未声明、比率引用未知度量 | 先声明被引用对象，或修正引用名 | `refs[0].dimension: dim_region` 未声明 |
| `DIM-CONTRACT-004` | error | `schema_version` 不是 `1` | 改为 `schema_version: 1` | 写成 `schema_version: 2` |
| `DIM-PATH-001` | error | 契约/数据源/数据集根目录/输出目录为 UNC 网络路径；路径越界、指向目录而非文件、命中系统敏感目录 | 只使用本地、数据集根目录内部文件与非敏感输出目录 | `source: ../../etc/passwd` |
| `DIM-PATH-002` | error | 契约或数据源文件不存在、不可读；`dataset.root` 不是目录 | 检查路径拼写、根目录与权限 | `source: dim_missing.csv` |

## 行数与数据源

| 代码 | 级别 | 触发条件 | 修复建议 | 示例 |
| --- | --- | --- | --- | --- |
| `DIM-LIM-001` | error | 所有源累计数据行超过 `dataset.max_rows`（默认 500000） | 缩减样例或明确提高上限后重跑 | 第 500001 行触发，退出码 `1` |
| `DIM-SRC-001` | error | 文件已解析到合法路径，但打开或读取时发生 IO 错误 | 检查权限、占用与文件完整性 | 文件被其他进程锁定 |
| `DIM-SRC-002` | error | CSV 列数与表头不一致、CSV 解析/编码错误；JSONL 非对象、单行 JSON 非法、非 UTF-8 | 修正分隔、引号、编码或 JSONL 行 | JSONL 某行是数组而非对象 |
| `DIM-SRC-003` | error | 表头为空、存在空列名或重复列名 | 为每列提供唯一非空表头 | CSV 表头为 `id,,amount` |
| `DIM-SRC-004` | warning | 维度或事实源没有任何数据行 | 补充样例，或确认空表是否符合预期 | 维表只有表头 |
| `DIM-SRC-005` | error | 维度声明属性在源中不存在，或声明类型与推断类型不兼容 | 修正 `attribute.type`、属性名或数据 | 声明 `integer`，样例却是 `"A-01"` |

事实度量列缺失、引用列缺失、粒度列缺失分别由 `DIM-MEA-001`、`DIM-REF-001`、`DIM-KEY-005` 报告，不复用 `DIM-SRC-005`。

## 键与粒度

| 代码 | 级别 | 触发条件 | 修复建议 | 示例 |
| --- | --- | --- | --- | --- |
| `DIM-KEY-001` | error | 事实表在声明粒度上存在重复非空键组合 | 去重，或把缺失维度加入 `grain` | 同一 `sale_id` 出现两次 |
| `DIM-KEY-002` | error | 维度代理键存在重复非空值 | 保证代理键唯一，重建维表 | 两个成员都是 `customer_key=7` |
| `DIM-KEY-003` | warning | 维度自然键存在重复非空值 | 确认是否 SCD2；本工具不展开历史版本 | 同一 `customer_id` 有两条记录 |
| `DIM-KEY-004` | error | 事实粒度、维度代理键或自然键存在空值 | 补齐键值；键列不得依赖隐式空值匹配 | 粒度列 `sale_id` 为空 |
| `DIM-KEY-005` | error | 契约声明的粒度、代理键或自然键列在源中不存在 | 补列或修正契约列名 | `grain: [sale_id]`，源中无此列 |

重复键检查排除空值；空值单独由 `DIM-KEY-004` 报告。重复组按值 token 排序，最多列 20 组样例。

## 引用完整性

| 代码 | 级别 | 触发条件 | 修复建议 | 示例 |
| --- | --- | --- | --- | --- |
| `DIM-REF-001` | error | 事实外键列或维表被引用键列不存在 | 补齐列或修正 `fact_key` / `dimension_key` | 维表没有 `product_key` |
| `DIM-REF-002` | warning / error | 事实键非空，但不在维表成员中，且不在 `unknown_members` 白名单 | 补维成员、修正事实键，或显式登记白名单 | 事实引用 `customer_key=99`，维表无 99 |
| `DIM-REF-003` | error | 必填引用（`optional: false`）的事实外键存在空值 | 补齐外键；确属可空时声明 `optional: true` | 事实行 `date_key` 为空 |
| `DIM-REF-004` | error | 雪花维父维不存在、列缺失、子键为空、父成员缺失、父连接键重复 | 修正 `snowflake` 映射并补齐父子成员 | 子维 `line_key` 在父维找不到 |
| `DIM-REF-005` | error | 雪花维 `parent` 关系形成有向环 | 删除环中的一条父引用 | `dim_a → dim_b → dim_a` |

`unknown_members` 中的值只用于把孤儿降级/豁免，不会被视为与维表匹配，也不会增加 `matched_rows`。

## 连接放大

| 代码 | 级别 | 触发条件 | 修复建议 | 示例 |
| --- | --- | --- | --- | --- |
| `DIM-FAN-001` | warning | 维表连接键存在重复，且事实引用了重复键；实测放大率大于 1，风险为 `medium` 或 `high` | 先修复维键唯一性，再按事实粒度重算度量 | 一个 `product_key` 对应两行维记录 |

风险分级：

- `none`：维连接键无重复；
- `low`：有重复键但事实未引用，或放大率未能测得；
- `medium`：存在事实引用重复键，实测放大率大于 1 且小于 2；
- `high`：实测放大率大于等于 2。

只有 `medium` / `high` 产生发现项。报告指标中保留每个 join 的估算放大率、实测放大率、额外行数与重复键样例。

## 度量与聚合安全

| 代码 | 级别 | 触发条件 | 修复建议 | 示例 |
| --- | --- | --- | --- | --- |
| `DIM-MEA-001` | error | 非比率度量的 `column` 缺失，或源中不存在该列 | 补列或修正 `measure.column` | 度量声明 `amount`，源中无此列 |
| `DIM-MEA-002` | error | 可加性声明与聚合方式冲突 | `additive`→`sum`；`non_additive` 禁用 `sum`；`ratio`→`ratio` | `additive` 配 `avg` |
| `DIM-MEA-003` | error | 半可加度量未声明 `semi_additive_axis`，或轴不是本事实引用的维度 | 绑定真实引用的日期类维度 | 轴写成未引用的 `dim_region` |
| `DIM-MEA-004` | error | 比率度量的分子或分母在事实内不存在 | 声明分量并确保名称正确 | 分母指向 `qty`，但事实无此度量 |
| `DIM-MEA-005` | error | `sum` / `avg` 度量列为空类型、字符串或混合类型 | 改用数值列，或改用 `count`/`count_distinct` | 对 `station_name` 求和 |
| `DIM-MEA-006` | warning | `additive` / `semi_additive` 度量列含空值 | 明确空值语义；不要默认填 0 | 金额 3 行为空 |
| `DIM-MEA-007` | warning | 比率分量不是 `additive`，或分母列含 0 | 先使分量可加；处理零分母或用可定义的业务口径 | 分母 `rides` 有 0 值 |

安全矩阵对每个度量给出 `safe_to_sum`、`safe_aggregations`、`dtype` 与原因。比率度量的推荐路径是“先聚合分子、聚合分母，再派生比率”，永远不直接对结果比率求和。

## 一致性与角色

| 代码 | 级别 | 触发条件 | 修复建议 | 示例 |
| --- | --- | --- | --- | --- |
| `DIM-CON-001` | error | 同一 `conformed_group` 内两维属性名集合不同 | 统一属性清单 | 基线有 `fiscal_year`，另一维没有 |
| `DIM-CON-002` | error | 同名属性在 conformed 组内类型不同 | 统一类型定义 | 一维 `date_key: integer`，另一维 `date_key: string` |
| `DIM-CON-003` | warning | 同名属性在样例中的取值域不同 | 核对成员与编码映射 | 一维有 `2026-01-01`，另一维没有 |
| `DIM-CON-004` | error | 同一事实重复引用同一维度，但存在未声明角色的引用 | 为每个引用声明唯一 `role` | 下单日期与发货日期都引用 `dim_date` |
| `DIM-CON-005` | error | 同一事实、同一维度出现重复角色名 | 角色名在事实内唯一 | 两个引用都写 `role: event_date` |
| `DIM-CON-006` | warning | 同一维度在部分事实声明角色、在另一些事实完全不声明 | 跨事实统一角色策略 | 一个事实用 `role: buyer`，另一个不用角色 |
| `DIM-CON-007` | error | 同一事实内一个角色名指向多个不同维度 | 每个角色名只对应一个维度 | `role: owner` 同时指客户与组织 |
| `DIM-CON-008` | warning | 事实表没有任何维度引用 | 至少声明一个引用，或确认事实无需维度 | 事实 `refs: []` |
| `DIM-CON-009` | info | 维度未被任何事实直接引用，也不在任何雪花父链上 | 删除未用维度或补充引用 | 声明了 `dim_channel`，无事实使用 |

角色规则由事实内和跨事实两部分组成。`conformed_group` 取字典序最小的成员作为比较基线；成对比较属性集、类型与样例取值域。

## 聚合路径

事实可通过 `facts[].aggregation_paths` 声明“某度量沿一组维度角色汇总”的路径。契约阶段的 `DIM-AGG-001..007` 在加载时阻断（退出码 `1`）；裁定阶段的 `DIM-AGG-010..013` 进入报告与台账。

| 代码 | 级别 | 触发条件 | 修复建议 | 示例 |
| --- | --- | --- | --- | --- |
| `DIM-AGG-001` | error | `aggregation_paths` 不是列表、项不是映射、`name`/`measure` 为空、`axes` 为空或轴不是非空字符串 | 按契约参考补齐结构 | `axes: []` |
| `DIM-AGG-002` | error | 同一事实内聚合路径 `name` 重复 | 路径名在事实内唯一 | 两条都叫 `by_station` |
| `DIM-AGG-003` | error | `measure` 不是本事实已声明的度量 | 修正度量名或先声明度量 | `measure: missing_metric` |
| `DIM-AGG-004` | error | 轴既不是任何引用的 role，也不是本事实唯一引用的维度名 | 写 role 或维度名；先声明 ref | `axes: [dim_region]` 但事实未引用 |
| `DIM-AGG-005` | error | 轴有歧义：同一维度被重复引用却用维度名定位，或 spec 同时命中多个 role | 重复引用时必须显式写唯一 role | 两个日期引用都写 `axes: [dim_date]` |
| `DIM-AGG-006` | error | 同一路径内重复轴，或多个轴解析到同一引用 | 每个轴对应不同引用 | `axes: [service_date, service_date]` |
| `DIM-AGG-007` | error | `retain` 不是 `inner` / `left` | 只能用两种保留语义 | `retain: outer` |
| `DIM-AGG-010` | warning | 裁定为 `amplified` 或 `mixed`：至少一行贡献超过一次 | 修复维键唯一性或调整轴与保留语义 | `left` 下一个天然键匹配两行 |
| `DIM-AGG-011` | warning | 裁定为 `incomplete`：路径丢弃了事实行且无放大 | 改用 `left`，或修复孤儿外键 | `inner` 下多轴均无匹配 |
| `DIM-AGG-012` | error | 裁定为 `measure_blocked`：比率、非可加，或路径含半可加禁止轴 | 先聚合分子/分母再派生，或改用合法轴 | 对 `ratio` 度量做 `sum` 路径 |
| `DIM-AGG-013` | error | 裁定为 `indeterminate`：缺列、缺粒度列或前置度量/数据检查不可靠 | 补列并先修复 `DIM-MEA-*` / 空源问题 | 度量列不在事实表中 |

裁定以事实行**行序号**为身份，绝不使用可能不唯一的粒度键做内部主键。每条路径在 `metrics.aggregation_paths.paths["事实.路径"]` 中给出 `verdict`、`counts`（`fact_rows`、`participating_rows`、`lost_rows`、`amplified_rows`、`total_contributions`）、`prerequisite` 与按稳定行序号定位的有限证据；同时写入 SQLite `aggregation_path_runs` 表，可按 `run_id` + `path_name` 回查每次运行的结论。

`DIM-FAN-001` 仍按单个事实—维引用独立分级，不因路径裁定而改变；路径裁定是同一度量与一组轴的联合结论，二者互补而非替代。

## 运行边界

| 代码 | 级别 | 触发条件 | 修复建议 | 示例 |
| --- | --- | --- | --- | --- |
| `DIM-IO-001` | error | 输出目录无法创建/写入，或复核台无法绑定端口 | 检查 `--out` 权限、磁盘空间与端口占用 | 报告写到只读目录 |
| `DIM-CLI-001` | error | 子命令、选项或参数值非法 | 运行 `python -m dimcheck --help` | 给 `serve` 传非数字端口 |
| `DIM-EXPLAIN-001` | error | `explain` 收到不存在的错误码 | 不带参数运行 `explain` 查看列表 | `explain DIM-XXX-999` |
| `DIM-INTERNAL-001` | error | 未预期的异常到达 CLI 边界 | 保留输入、命令与堆栈反馈维护者 | 运行期程序错误 |

## 报告与台账

发现项写入 `report.json` 的 `findings`、`report.md` / `report.html` 的发现段，以及 `ledger.sqlite3` 的 `findings` 表。每个发现项含：

- `code`、`severity`、`object_type`、`object_name`；
- `message`（说明）与 `fix`（修复建议）；
- `evidence`（结构化证据，稳定排序）。

同一错误码可对多个对象重复出现；`summary.by_code` 给出计数。持续集成可基于退出码判定，也可解析 JSON 的 `findings` 做更细粒度门禁。
