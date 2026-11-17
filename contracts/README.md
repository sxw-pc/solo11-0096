# 模型契约参考

契约是一个 YAML 映射，描述星型模型的维度、事实粒度、引用关系、度量语义与一致性约束。加载后会做结构校验、规范化，并计算规范 JSON 的 SHA-256 指纹。契约文件不含任何执行代码。

## 最小示例

```yaml
schema_version: 1
run_epoch: 1767225600
dataset:
  name: example_dw
  root: ../fixtures
  max_rows: 500000
dimensions:
  - name: dim_customer
    source: dim_customer.csv
    format: csv
    surrogate_key: customer_key
    natural_keys: [customer_id]
    attributes:
      - {name: customer_key, type: integer}
      - {name: customer_id, type: string}
facts:
  - name: fct_sales
    source: fct_sales.csv
    format: csv
    grain: [sale_id]
    refs:
      - dimension: dim_customer
        fact_key: customer_key
        dimension_key: customer_key
        role: buyer
    measures:
      - name: amount
        column: amount
        agg: sum
        additivity: additive
```

## 顶层字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema_version` | 是 | 当前仅支持 `1` |
| `run_epoch` | 是 | 正整数 Unix 秒；`SOURCE_DATE_EPOCH` 缺省时的报告时间 |
| `dataset` | 是 | 数据集元信息 |
| `dimensions` | 是 | 至少一个维度 |
| `facts` | 是 | 至少一个事实 |

### `dataset`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `name` | 必填 | 数据集名，进入 `run_id` |
| `root` | `.` | 数据源根目录；相对契约文件所在目录解析 |
| `max_rows` | `500000` | 所有源文件合计数据行上限 |
| `null_tokens` | `["", "null", "NULL"]` | 额外归一为空值的字符串；空字符串始终归一为空 |

### `dimensions[]`

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 全局唯一的实体名 |
| `source` | 是 | 相对 `dataset.root` 的 CSV/JSONL 路径；绝对路径也必须在根目录内 |
| `format` | 否 | `csv` / `jsonl`；缺省按扩展名推断 |
| `surrogate_key` | 是 | 代理键列名 |
| `natural_keys` | 否 | 自然键列名列表 |
| `attributes` | 是 | 非空属性列表，每项含 `name`、`type` |
| `conformed_group` | 否 | 一致性维度组名 |
| `snowflake` | 否 | 雪花父维映射，见下 |

`attributes[].type` 允许：`integer`、`float`、`string`、`boolean`、`date`。类型检查基于样例推断；`string` 接受任意标量，`float` 接受整数或浮点。

`snowflake` 结构：

```yaml
snowflake:
  parent: dim_line
  child_key: line_key
  parent_key: line_key
```

父维度必须已声明。父键应唯一；子维每行应能在父维中找到对应成员。父引用环会报 `DIM-REF-005`。

### `facts[]`

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 全局唯一的实体名 |
| `source` | 是 | 数据源路径，规则同维度 |
| `format` | 否 | `csv` / `jsonl` |
| `grain` | 是 | 非空且不重复的列名列表，用于粒度唯一性核验 |
| `refs` | 否 | 维度引用列表 |
| `measures` | 是 | 非空度量列表 |
| `aggregation_paths` | 否 | 事实级聚合路径声明；缺省不启用，见下 |

#### `refs[]`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `dimension` | 必填 | 已声明维度名 |
| `fact_key` | 必填 | 事实表列 |
| `dimension_key` | 必填 | 维表列 |
| `role` | 无 | 角色扮演名；同一事实多次引用同一维度时必须声明 |
| `optional` | `false` | 为 `true` 时空外键不报 error；孤儿键降为 warning |
| `unknown_members` | `[]` | 允许的未知维成员白名单；不参与孤儿统计，也不视为匹配连接 |

角色规则：

- 同一事实内重复引用同一维度，必须全部显式声明 `role`，且角色唯一。
- 不同维度不能在同一事实中共享角色名。
- 同一维度若在部分事实中声明角色、在另一些事实中不声明，会产生一致性 warning。
- 维表被多个事实直接引用不会自动要求角色；角色要求聚焦于重复引用语义。

#### `measures[]`

| 字段 | 说明 |
| --- | --- |
| `name` | 事实内唯一 |
| `column` | 源列；`additivity: ratio` 时可省略 |
| `agg` | `sum` / `avg` / `min` / `max` / `count` / `count_distinct` / `ratio` |
| `additivity` | `additive` / `semi_additive` / `non_additive` / `ratio` |
| `unit` | 可选单位标签 |
| `semi_additive_axis` | 半可加度量必填；必须是事实引用的维度名 |
| `numerator` / `denominator` | 比率度量必填，引用同事实的度量名 |

约束：

- `additive` 对应 `agg: sum`。
- `semi_additive` 使用 `sum`，但只允许沿非 `semi_additive_axis` 维度求和；工具在矩阵中标记为不可全局求和。
- `non_additive` 不得使用 `sum`。
- `ratio` 必须 `agg: ratio`，且分子、分母存在；工具不直接对比率列求和。
- `sum` / `avg` 的源列必须推断为数值类型。

#### `aggregation_paths[]`（可选）

事实级聚合路径声明：一条路径把某个度量沿一组维度角色汇总，并声明连接保留语义。工具只按**声明的路径**在样例数据上推演贡献次数，不读取、不推断、不执行任何用户 SQL。缺省不声明即完全保持旧契约行为。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 事实内唯一的路径名；报告与台账以 `事实名.路径名` 定位 |
| `measure` | 是 | 本事实已声明的度量名 |
| `axes` | 是 | 非空的分组轴列表，每项解析到本事实的**唯一引用**（见下） |
| `retain` | 是 | `inner`：任一轴无匹配则事实行不参与结果；`left`：无匹配保留一次空维成员贡献 |

轴解析规则：

- 轴先按 `refs[].role` 匹配；没有 role 且该维度在本事实只被引用一次时，允许直接用维度名。
- 同一维度在本事实被引用多次时必须显式写 role；否则报 `DIM-AGG-005`。
- 轴必须解析到本事实内的引用，未知轴报 `DIM-AGG-004`。
- 同一路径内不要重复轴，也不要让两个轴解析到同一个引用（`DIM-AGG-006`）。
- `name` 重复报 `DIM-AGG-002`，`measure` 未知报 `DIM-AGG-003`，结构问题报 `DIM-AGG-001`，`retain` 非法报 `DIM-AGG-007`。

示例：

```yaml
aggregation_paths:
  - name: revenue_by_fare_band_payment
    measure: revenue_amount
    axes: [fare_band, payment_method]   # role 名
    retain: inner
  - name: transactions_by_station_date
    measure: transaction_count
    axes: [station, accounting_date]
    retain: left
```

裁定语义（详见 [docs/CODES.md](../docs/CODES.md)）：

- 以事实行身份（行序号）为基准，按每个轴引用键在维表中的真实匹配数，推演多轴连接后**单行贡献次数**；`left` 下无匹配轴按一次空成员贡献计，因此另一轴的多匹配仍会放大。
- 仅当度量是 `additive` 且通过既有列/类型/空值检查时，才逐行计数：
  - 所有参与行恰好贡献一次 → `safe`；
  - 有行贡献超过一次且无丢失 → `amplified`；
  - 有行贡献为零且无放大 → `incomplete`；
  - 两者同时存在 → `mixed`（不把抵消后的行数相等当作安全）。
- 比率、`non_additive`、以及路径轴命中 `semi_additive_axis` 的半可加度量 → `measure_blocked`；不会因样例恰好只有一行而放行。
- 缺列、缺粒度列或前置度量/数据检查不可靠 → `indeterminate`，不当作 `safe`。

## 一致性维度

两个或多个维度可以共享 `conformed_group`。工具比较组内每对维度的：

1. 属性名集合；
2. 同名属性类型；
3. 同名属性在样例中的取值域（不一致为 warning）。

示例中的 `dim_date` 与 `dim_accounting_date` 属于 `calendar` 组，属性与取值域一致。

## 数据格式

- CSV：首行是唯一、非空的表头；每行列数必须与表头一致；UTF-8（允许 BOM）。
- JSONL：每行是一个 JSON 对象；不同行的键取并集，缺失字段为空；列顺序按名称稳定排序。
- 空字符串、`NULL`、`null` 与配置的 token 转为空值。
- 常见整数、浮点、`true` / `false` 与 `YYYY-MM-DD` 会被识别；其他数据保留为字符串。
- 数据行上限在所有源之间累计，不是每表独立上限。

## 路径与安全

- `source` 会做 `resolve()` 后检查是否仍在 `dataset.root` 内；`../` 或符号链接逃逸被拒绝，退出码 `1`。
- 契约文件、数据集根目录、数据源与输出目录均拒绝 UNC / 网络路径；数据集根目录与输出目录还拒绝系统敏感目录。
- 契约文件路径本身由 CLI 检查；系统敏感目录会被拒绝。
- 契约加载不执行 SQL、不解析源文件中的任何表达式。

## 契约错误

结构、类型、引用与版本错误会在加载阶段以 `DIM-CONTRACT-*` / `DIM-PATH-*` 阻断，退出码 `1`。数据、键、引用、聚合与一致性问题进入报告，按 error/warning/info 分级。完整目录见 [`docs/CODES.md`](../docs/CODES.md)。
