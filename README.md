# cc-retain-lab

CC_Codex 二期（26.11）**数据工程**产品线：**留存裁撤台**（FastAPI + 原生 JS SPA + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**分区留存策略版本 → 到期扫描裁决 → 归档或销毁执行 → 策略与实物分区对账**

与现有六线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取）。  
也不碰外仓常见指纹（Feed 归并 / 作业编排 / CDC 游标 / 质检签发 / 贴源-明细-汇总）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数仓与数据湖（分区留存 / 裁撤闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、FastAPI、Uvicorn、SQLite |
| 前端 | 原生 JS 单页（全中文），青灰底 + 铜强调，顶栏导航（非绿侧栏克隆） |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 现行留存策略（主题域 / 保留天 / 到期动作）  
2. 实物分区列表（在线 / 归档 / 销毁 / 裁撤中）  
3. 按基准日到期扫描 → 生成待执行裁决  
4. 单条裁决执行（归档写地址 / 销毁清零）  
5. 策略与实物对账报告  

预留切口（未做，供后续功能题）：策略换版生效窗、重裁作废、批量失败闭互斥、跨域依赖冻结、归档回灌等。

## 对账一致性（retain-05 增量）

**问题背景**：对账进行中另一人点裁撤，对账卡片在线数与分区列表瞬时数拧巴；
两名值班并发写还可能双写报告，让账实偏掉。本增量把计数口径与落笔并发钉死。

- **单一计数口径**：`count_partition_states()` 是唯一计数入口，一次 `GROUP BY`
  在同一读点取齐在线 / 已归档 / 已销毁 / 裁撤中，页面卡片、对账落库、库内核验都走它，禁止另写 `COUNT` 拼数。
- **读页冻结快照**：`GET /api/reconcile/snapshot` 在一个 `BEGIN` 只读事务内
  同时返回对账摘要卡、错位条、执行列表、分区详情——三块同动、同一 `snapshot_at`，
  不再各发请求取三个瞬时数。
- **落笔单读点 + 混数即拦**：`POST /api/reconcile-reports` 走 `BEGIN IMMEDIATE`
  单写事务，INSERT 前同事务复核、INSERT 后读回核对；四态合计 ≠ 分区总行数或
  复核漂移一律抛 `CountDriftError` 回滚，返回 409，库里不留脏报告。
- **两人写加锁**：进程内非阻塞互斥锁（`threading.Lock`）+ SQLite 即时写锁双层。
  扫描 / 裁撤 / 对账落库共用一把锁；后来者立刻收到 409「请稍后重试」，不排队双写；
  跨进程抢锁等满 `busy_timeout` 后同样翻译成 409 而非 500。
- **本机库可验**：报告落本机 SQLite（`instance/retain_lab.db`），历史报告是
  冻结快照（后续裁撤不污染旧数字）。直连库核验：

  ```bash
  python scripts/verify_reconcile_db.py            # 默认 instance/retain_lab.db
  python scripts/verify_reconcile_db.py --db 路径  # 任意库
  ```

  退出码 0 = 全部报告四态自洽、错位条数与库内实查一致；发现混数退出码 1。

### 关键接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/reconcile/snapshot` | 冻结快照：摘要 KPI / 错位条 / 执行列表 / 分区详情同源 |
| POST | `/api/reconcile-reports` | 写事务内对账落库，混数或锁冲突返回 409 |
| POST | `/api/judgments/{id}/execute` | 裁撤落笔，与对账共用写锁 |

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-retain-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6500/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6500**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
