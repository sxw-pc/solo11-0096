# 离线与合规说明

本文说明 `dimcheck` 的安全边界、可验证承诺与不承诺事项。目标是让工具适合在隔离环境、审计环境和不可联网的构建流水线中运行。

## 完全离线

- 运行期不导入 HTTP 客户端，不发起出站连接，不做 DNS 查询，不访问 CDN，不调用在线字体、图标或地图服务。
- 唯一使用网络栈的代码是 `serve` 的本地 HTTP 服务；它只绑定回环地址 `127.0.0.1`，不连接外部主机。
- 没有遥测、崩溃上报、使用统计、许可心跳或“检查更新”。
- 依赖安装本身可能由环境完成；安装完成后，`demo`、`run`、`explain`、`serve` 均不需要网络。运行期第三方依赖只有 `PyYAML`。
- 测试带 autouse socket 守卫：非回环 DNS 查询会直接失败，避免无意引入联网行为。

## HTML 自包含

`report.html`：

- 内联全部 CSS；
- 不包含 `<script>`、外链脚本、外链样式表、远程字体或图标；
- 不包含 `<img>` 外部地址、iframe、object 或 embed；
- 不使用 `url(...)` 拉取资源；
- 报告正文只依赖同一份 JSON 对象中的本地数据，文本经过 HTML 转义。

复核台额外发送严格响应头：

- `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; img-src data:; ...`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`
- `Cache-Control: no-store`

## 本地数据边界

- 契约路径由用户显式给出；CLI 检查文件存在，并拒绝明显敏感的系统目录（如 Unix 的 `/etc`、`/proc`、`/sys`、`/root`，以及 Windows 系统根目录）。
- `dataset.root` 是数据边界。每个 `source` 解析后必须仍位于该根目录内；`../` 逃逸与符号链接逃逸均被拒绝，返回 `DIM-PATH-001` 与退出码 `1`。数据集根目录本身也会拒绝系统敏感目录。
- UNC 与网络共享路径在契约、数据集根目录、数据源、契约文件和输出目录边界均被拒绝，避免把“本地运行”变成网络访问。
- 只接受 `csv` 与 `jsonl`；不执行 SQL，不加载模块，不解析数据中的表达式。
- 源文件只读。工具不修复、不转换、不回写源数据。
- 单次运行所有源累计行数默认上限 `500000`，超限为 `DIM-LIM-001`、退出码 `1`；可在契约中显式调整。
- 路径守卫是针对明显越界与敏感路径的防护，不是操作系统级沙箱。不可信输入仍应在容器或受限账户中运行。

## 输出与台账

- `run` 的所有生成物均落在 `--out`（默认 `./out`），包括报告、`run.json` 与 `ledger.sqlite3`。`demo` 是唯一例外：它按固定 seed 重建仓库内 `fixtures/`，以便从零复现示例。工具不会在用户主目录隐式创建应用目录或配置文件。
- 输出目录必须不是敏感系统目录；创建或写入失败映射为 `DIM-IO-001`，退出码 `1`。
- 重复运行会更新同名报告文件，这是产物语义；历史不靠报告文件保存，而靠台账保存。
- 台账只追加：`runs`、`findings`、`metric_snapshots` 均使用 INSERT，运行主键自增。工具不删除历史，不覆盖旧记录。
- 台账中的 `started_at` 与 `duration_ms` 允许每次不同；`report.json` 的正文时间来自 `SOURCE_DATE_EPOCH` 或契约，不来自当前时钟。
- `report.json`、`report.md`、`report.html` 在相同工具版本、相同输入与相同 `SOURCE_DATE_EPOCH` 下逐字节一致。`run.json` 的两个运行元数据字段是明确豁免项；SQLite 文件本身不作为逐字节可复现产物。

## 时间与确定性

- 报告 epoch 优先取环境变量 `SOURCE_DATE_EPOCH` 的正整数值；否则取契约的 `run_epoch`。
- 报告正文不调用 `datetime.now()`；当前时钟只用于 `run.json` 的 `started_at` 和性能计时。
- 规范 JSON 使用 `ensure_ascii=False`、`sort_keys=True`、固定分隔符与缩进；指纹是该规范 JSON 的 SHA-256。
- 所有发现项、样例、矩阵、画像、指标均按固定键排序；不使用集合的迭代顺序写报告。
- 示例 fixture 使用固定 seed 生成，重复执行得到相同数据。

## 密钥与敏感标识

仓库内的契约、fixture、文档与测试：

- 不包含真实密钥、令牌、私钥、证书、口令或连接串；
- 不包含真实公司名、组织名、域名、站点地址或电子邮件地址；
- 不包含真实 IP；唯一允许并实际出现的地址字面量是回环地址 `127.0.0.1` 与主机名 `localhost`；
- 示例城市、线路、站点、渠道、客户与产品均为虚构代号；
- 不使用真实业务数据或可识别个人信息。

工具不读取云凭据、不读取 `.env`、不扫描环境变量中的密钥。除路径守卫所需的 `SystemRoot` 与报告时间所需的 `SOURCE_DATE_EPOCH` 外，不依赖环境变量。

## 复核台

`serve` 是本地只读查看器，不是多用户 Web 应用：

- 仅绑定 `127.0.0.1`，默认端口 `8765`；
- 路由白名单：`/`、`/report.json`、`/report.md`、`/report.html`、`/run.json`、`/ledger.json`；
- 只支持 GET 与 HEAD；其他方法返回 405；
- 不提供上传、删除、配置修改或任何写接口；
- 不进行认证与授权，因此只应在单人受信本机使用，不应暴露到局域网或公网；
- 路径只从白名单映射，不接受任意文件系统路径；
- 错误不泄露绝对路径之外的额外信息。

## 依赖与供应链

- Python 3.10+。
- 运行期第三方依赖：`PyYAML`。
- 测试依赖：`pytest`。
- 显式不使用 pandas、pyarrow、pydantic、jinja2、requests 等重型或联网依赖。
- YAML 使用 `SafeLoader` 派生加载器；额外拒绝重复键，避免静默覆盖。
- 所有随机性只存在于 fixture 生成，且使用固定 seed；运行期报告不使用随机数。

## 可验证清单

| 承诺 | 验证方式 |
| --- | --- |
| 离线 | 阅读导入；运行测试（含 socket DNS 守卫） |
| HTML 无外部资源 | 检查 `report.html` 无 `http://`、`https://`、`<script>`、外链标签；运行 `test_report` |
| 路径逃逸拒绝 | `test_contract` / `test_cli` 中的越界路径用例 |
| 行数上限 | `test_sources` / `test_cli` 中的超限用例 |
| 台账追加 | `test_ledger` 连续两次运行计数为 2 |
| 逐字节确定性 | `test_determinism` 比较两个输出目录的报告字节 |
| 只读复核台 | `test_server` 检查 GET、405、CSP 与 404 |
| 行数预算 | `python scripts/check_limits.py` 与 `test_limits` |
| 无真实地址/密钥 | 仓库文本检索；仅 `127.0.0.1` 为地址字面量 |

## 明确不承诺

- 不证明生产全量数据满足约束；检查基于契约和本地样例。
- 不提供 SQL 查询级审计、执行计划分析或运行时拦截。
- 不自动修复模型、重写 ETL、生成维表或处理 SCD2。
- 不替代访问控制、密钥管理、网络隔离或数据脱敏系统。
- 不对第三方依赖本身的安全性作保证；隔离环境应按组织策略锁定和审计依赖。
- 复核台不提供 TLS、认证或多租户隔离。
