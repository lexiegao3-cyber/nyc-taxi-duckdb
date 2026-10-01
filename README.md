# 纽约市出行需求分析 · DuckDB

基于 [DuckDB](https://github.com/duckdb/duckdb) 的纽约市出租车 / 网约车出行需求分析。数据直接来自
[NYC TLC 官方下载页](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page) 按月发布的
Parquet 行程文件和出租车区域（Taxi Zone）对照表。

第一版技术栈：**Python + DuckDB + 简单网页界面**（FastAPI + 原生 JS + ECharts）。
整个系统只有 **一个进程** 打开数据库文件，所有写入在该进程内由 **唯一的写线程** 串行执行。

![tests](https://github.com/lexiegao3-cyber/nyc-taxi-duckdb/actions/workflows/ci.yml/badge.svg)

## 为什么能体现 DuckDB 的大数据处理能力

在一台 Apple M4（10 核，16 GB 内存）笔记本上，导入 2025 年 1–3 月黄车 + 绿车 + 网约车（Uber/Lyft）数据：

| 指标 | 结果 |
| --- | --- |
| 原始 Parquet | 9 个文件，约 1.6 GB |
| 导入后行数 | **71,402,965** 行（清洗掉异常记录后） |
| 导入耗时 | **约 9 秒**（9 个文件，单个 2000 万行文件约 2 秒，约 1000 万行/秒） |
| 全表条件计数 | 44 ms（约 16 亿行/秒） |
| 按小时聚合 7100 万行 | 77 ms |
| 区域 × 日 分组聚合 | 159 ms |
| 精确中位数 / P90 / P99（按服务分组） | 1.8 s |
| 异常需求检测（区域-小时窗口 + 留一法基线） | 约 0.5 s |

> 数字来自页面“性能基准”标签 / `python -m taxi bench`，会因机器而异。

用到的 DuckDB 能力：

- **直接读取 Parquet**：`read_parquet()` + `parquet_file_metadata()`，无需任何 ETL 工具；“性能基准”页还会把同一条 SQL 直接跑在原始 Parquet 上做对比。
- **向量化 + 多线程执行**：所有分析都是对完整明细表的实时 SQL，没有预聚合表。
- **分析型 SQL**：窗口函数（7 日移动平均、份额、留一法基线）、`QUALIFY`、`GROUP BY ALL`、`FILTER`、精确 `quantile_cont`、`count(DISTINCT (a, b))`。
- **MVCC**：导入新月份数据时，查询继续读取上一个已提交快照，不会看到“导入一半”的数据（见 `tests/test_db.py::test_reads_continue_during_writes`）。
- **`EXPLAIN ANALYZE`**：每个图表右上角的 “SQL” 按钮可以查看 SQL 和实际执行计划。
- **解析树级别的安全检查**：SQL 实验室用 DuckDB 自带的 `json_serialize_sql` 解析查询，只允许读取 `trips` / `zones` / `ingested_files`。

## 单写入进程架构

```
           浏览器 (index.html + ECharts)
                     │  HTTP
┌────────────────────▼──────────────────────────────────────┐
│  python -m taxi serve   ← 唯一打开 taxi.duckdb 的进程          │
│                                                           │
│  FastAPI 线程池 ── 每个请求一个 cursor() ──► 并发只读查询        │
│                                         (MVCC 快照)        │
│  下载线程 ×3 ── 并行下载 Parquet（只写各自的文件）               │
│        │ 下载完成                                           │
│        ▼                                                  │
│  写线程 ×1 (ThreadPoolExecutor(max_workers=1))             │
│        └─ BEGIN; DELETE 该月; INSERT … SELECT read_parquet; COMMIT │
└───────────────────────────────────────────────────────────┘
```

- DuckDB 同一时间只允许一个进程以读写方式打开数据库文件；因此 Web 服务就是唯一的写入者，导入通过 `POST /api/ingest` 交给它完成，而不是另起进程写库。
- 服务运行时若再执行 `python -m taxi ingest`，会得到明确提示（`DatabaseLockedError`），而不是破坏文件。
- 每个（服务, 月份）的导入是一个事务：先删除旧数据再插入，可重复执行（幂等）。
- 每次提交后 `Database.version` 自增，分析结果缓存以它为键，导入后自动失效。

## 分析内容

| 标签页 | 分析 |
| --- | --- |
| 需求概览 | 关键指标、每日出行量 + 7 日移动平均、星期 × 小时热力图、各公司月度份额、行程距离分布 |
| 空间分析 | 热门上车区域、行政区间流向（桑基图）、热门 OD、JFK / LGA / EWR 进出机场时段分布 |
| 运营与效率 | 每英里 / 每分钟票价、小费率、司机分成、等车时间、曼哈顿拥堵费（2025-01-05 起）覆盖率；分时段车速（拥堵）；网约车等车时间 P50/P90 |
| 事件检测 | 区域-小时需求相对“同区域·同星期·同小时”留一法基线的 Z 分数。在 2025 Q1 数据中排在前面的峰值与已知事件高度吻合：跨年夜凌晨的布鲁克林 / 曼哈顿、2 月 9 日超级碗当晚的 Battery Park City、3 月 13–14 日普珥节期间的 Borough Park / South Williamsburg |
| 性能基准 | DuckDB 表 vs 直接查询原始 Parquet |
| SQL 实验室 | 只读 SQL，自带示例 |
| 数据导入 | 选择服务和月份，从 TLC 下载并导入，实时显示进度 |

所有分析支持按服务类型、日期范围、上车行政区筛选。

## 快速开始

需要 Python 3.10+。

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

方式一：先用命令行导入（此时服务未运行），再启动网页：

```bash
python -m taxi ingest --services yellow,green --months 2025-01:2025-03
python -m taxi serve            # 打开 http://127.0.0.1:8000
```

方式二：直接启动网页，在“数据导入”标签页里选择服务和月份导入（导入期间可以继续浏览分析）。

加入网约车（每月约 2000 万行、500 MB）来体验更大的数据量：

```bash
python -m taxi ingest --services yellow,green,fhvhv --months 2025-01:2025-03
```

其他命令：

```bash
python -m taxi download --services fhvhv --months 2025-01   # 只下载
python -m taxi bench                                        # 性能基准（服务需停止）
```

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TAXI_DATA_DIR` | `data` | 原始 Parquet（`data/raw/`）和数据库存放位置 |
| `TAXI_DB_PATH` | `data/taxi.duckdb` | 数据库文件 |
| `TAXI_THREADS` | 全部核心 | DuckDB 线程数 |
| `TAXI_MEMORY_LIMIT` | DuckDB 默认（80% 内存） | 例如 `8GB` |
| `TAXI_DOWNLOAD_WORKERS` | `3` | 并行下载数 |
| `TAXI_BASE_URL` | TLC CloudFront 地址 | 可指向镜像，也支持 `file://` |

## 数据处理

`taxi/loader.py` 把三种文件规范化为一张 `trips` 表：

| 字段 | 黄车 / 绿车 | 网约车 (HVFHV) |
| --- | --- | --- |
| `pickup_at` / `dropoff_at` | `tpep_*` / `lpep_*` | `pickup_datetime` / `dropoff_datetime` |
| `company` | Yellow / Green | 由 `hvfhs_license_num` 映射为 Uber / Lyft / Via / Juno |
| `trip_miles` | `trip_distance` | `trip_miles` |
| `fare` | `fare_amount` | `base_passenger_fare` |
| `total` | `total_amount` | 车费 + 过路费 + 各项附加费 + 税 + 小费 |
| `wait_min`、`driver_pay`、`shared` | — | 叫车到上车时间、司机收入、拼车 |

TLC 的列名和类型在不同年份间会变化（如 `Airport_fee` / `airport_fee`、2025 年新增 `cbd_congestion_fee`），
所以 SELECT 语句根据每个文件实际的列生成，缺失的列补 NULL。

清洗规则：上车时间必须落在文件所属月份内；行程时长在 0–6 小时之间；区域编号 1–265；里程 0–200 英里；实付金额非负。
`ingested_files` 表记录每个文件的原始行数、导入行数和耗时。

## 项目结构

```
taxi/
  config.py      环境变量配置
  sources.py     TLC 文件命名规则、月份解析、原子下载
  loader.py      表结构 + 各服务的规范化 SQL
  db.py          Database：单写线程 + 并发只读 cursor
  ingest.py      并行下载 → 串行导入 的任务流水线
  analytics.py   全部分析 SQL 和基准查询
  server.py      FastAPI 接口
  static/        网页界面
tests/           用 DuckDB 生成的合成 Parquet，离线运行
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

测试用 DuckDB 生成与 TLC 结构一致的合成 Parquet 文件（含应被清洗掉的脏数据），通过 `file://` 地址走完整的“下载 → 导入 → 查询”流程，
覆盖：清洗规则、重复导入幂等、写入期间并发读取的快照一致性、每个分析在有/无筛选条件下的执行、缓存失效、SQL 实验室的安全限制。

## 路线图

- 第一版（当前）：单进程单写线程、实时 SQL 分析、网页界面
- 后续：出租车区域 GeoJSON 地图、更多年份的同比分析、天气数据关联、导出分析结果为 Parquet

## 许可

MIT。TLC 行程数据版权归纽约市出租车与豪华轿车委员会所有，使用须遵守其条款。
