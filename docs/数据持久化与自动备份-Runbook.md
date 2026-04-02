# 数据持久化与自动备份 · 运维 Runbook

> **依据**：`docs/数据持久化与自动备份-方案.md`  
> **实现**：`scripts/cloud_backup/backup_lib.py`、`scripts/cloud_backup/backup_run.py`、`.github/workflows/cloud_backup.yml`  
> **适用范围**：**远程 Postgres（如 Neon）+ Cloudflare R2**；本地 Sqlite/JSON **不适用**本流水线（见方案「〇、适用范围」）。

---

## 〇、将 `cloud_backup.yml` 推送到 GitHub

若使用 **Personal Access Token** 推送代码，需勾选 **`workflow`** 权限，否则远端会拒绝更新 `.github/workflows/cloud_backup.yml`。  
无该权限时：可先在本地保留该文件，换用带 `workflow` 的 PAT、SSH，或在网页上 **New file** 粘贴工作流内容后再启用 Actions。

---

## 一、GitHub Secrets（仓库 Settings → Secrets and variables）

| Secret | 说明 |
|--------|------|
| `DATABASE_URL_BACKUP` | **备份专用** Postgres 连接串；`pg_dump` 长任务请用 **直连/非池化** 端点（与 App 用 Pooler 串区分，见方案 §4.3）。 |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 S3 兼容 API 密钥；权限应 **仅限** `backups/` 前缀（或独立备份桶）的 List/Get/Put/Head。 |
| `R2_BUCKET_NAME` | 桶名。 |
| `R2_ACCOUNT_ID` | Cloudflare 账户 ID；用于拼默认端点 `https://{id}.r2.cloudflarestorage.com`（可与 `R2_ENDPOINT_URL` 二选一）。 |
| `R2_ENDPOINT_URL` | 可选；若已填完整端点则可不填 `R2_ACCOUNT_ID`。 |
| `BACKUP_ALLOW_SIZE_DROP_OVERRIDE` | **慎用**：仅当 Postgres  dump 体积相对上一份成功备份下跌超过熔断阈值、且确认为合法删数/归档时，设为固定字面量 `OVERRIDE_SIZE_DROP_CONFIRMED`；须在别处留 **审计记录**（操作者、时间、原因、`backup_run_id`），见方案 §7.3。 |

### Repository variables（可选，Settings → Variables）

| Variable | 默认 | 说明 |
|----------|------|------|
| `BACKUP_R2_PREFIX` | `backups` | 冷备对象统一前缀（勿与业务对象根前缀混用）。 |
| `R2_DATASET_PREFIXES` | 空 | 英文逗号分隔，如 `media/,uploads/`；**勿**填 `backups/`。空则仅备份 Postgres，不在桶内做业务对象清单/镜像。 |
| `R2_DATASET_MODE` | `manifest_only` | `manifest_only`：仅写清单 JSON 至 `backups/inventory/`；`prefix_copy`：同桶 Copy 至 `backups/r2_mirror/{run_id}/`。 |
| `BACKUP_RETENTION_COMPLETED` | `7` | 仅 **`run_status=completed`** 的 run 参与保留计数；淘汰 **仅**在成功 run 完成后执行。 |
| `BACKUP_SIZE_DROP_PCT` | `30` | 新 dump 较上一份成功 dump 体积下降超过该百分比则 **失败** 且不更新「最新好备份」指针。 |
| `BACKUP_DEPLOYMENT_ENV` | `prod` | 写入 manifest 的 `deployment.env`。 |
| `BACKUP_DB_HOST_HINT` | 空 | manifest 中 `deployment.db_host` 非敏感标识（**禁止**写口令）。 |

---

## 二、对象布局（R2）

- `backups/pg/{backup_run_id}/postgres.sql.gz`：压缩后的 `pg_dump` 明文。  
- `backups/manifests/{backup_run_id}.json`：总 manifest（恢复时 **第一步** 校验此文件）。  
- `backups/manifests/latest_completed.json`：指向最近一次 **completed** run 的指针。  
- `backups/inventory/{backup_run_id}.json`：业务对象清单（配置了 `R2_DATASET_PREFIXES` 时）。  
- `backups/r2_mirror/{backup_run_id}/...`：`R2_DATASET_MODE=prefix_copy` 时的镜像副本。

---

## 三、恢复顺序（与方案 §9.1 对齐）

1. 下载并校验目标 `backup_run_id` 的 **manifest**（字段齐全、与桶内对象一致）。  
2. 按 `postgres.object_key` 拉取 `postgres.sql.gz`（明文 SQL + gzip）。在 **空库或 Neon branch** 上解压后执行 `psql` 导入（**勿**直接覆盖未冻结的生产库）；本实现未使用 `pg_dump -Fc` 自定义格式。  
3. 只读校验：关键表行数、抽样查询。  
4. **调和**：按方案第三节区分「整站回滚」与「仅修裂脑」，再处理幽灵对象与悬空记录。  
5. 验证通过后 **切流**（连接串或提升 branch）。  

**禁止**：半套 run 当成功使用；失败 run **不参与**淘汰旧世代。

---

## 四、本地试跑（需本机 `pg_dump` 与网络可达 DB/R2）

```bash
export DATABASE_URL_BACKUP="postgresql://..."
export R2_ACCOUNT_ID="..."
export R2_ACCESS_KEY_ID="..." R2_SECRET_ACCESS_KEY="..." R2_BUCKET_NAME="..."
pip install -r requirements-backup.txt
python scripts/cloud_backup/backup_run.py
```

退出码：`0` 成功，`1` 失败，`2` 跳过（未配置库或 R2）。在 **GitHub Actions** 中 `2` 会映射为 `0`，以便未配置 Secrets 时 job 仍为绿（仅打印跳过原因）。

---

## 五、与业务/Agent 文档的边界

冷备 **不替代** `docs/项目记忆持久化与Agent读取策略.md` 中的记忆写入与 Agent 读取契约；二者验收 **分开**。
