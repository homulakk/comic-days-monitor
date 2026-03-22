# Comic-Days 漫画监控脚本

自动监控多部漫画更新，下载最新话。

## 特点

- **纯 HTTP API**：无需浏览器，资源占用低
- **多漫画监控**：支持同时监控多部作品
- **智能购买**：票据优先，PT 补充
- **增量下载**：基于 `purchased` 字段去重，不重复下载
- **PT 维护**：直接在 `accounts.json` 中维护 PT 余额
- **Session 缓存**：避免频繁登录触发封禁
- **原子写入**：防止文件损坏

## 安装

```bash
# 克隆仓库
git clone https://github.com/homulakk/comic-days-monitor.git
cd comic-days-monitor

# 安装依赖
pip install -r requirements.txt
```

## 配置

### 1. 账号池

复制模板并填入账号：

```bash
cp accounts.json.example accounts.json
```

编辑 `accounts.json`：

```json
{
  "accounts": [
    {
      "email": "your_email@test.com",
      "password": "your_password",
      "pt": 100
    }
  ],
  "updated_at": "2026-03-22T15:00:00"
}
```

| 字段 | 说明 |
|------|------|
| `email` | 登录邮箱 |
| `password` | 登录密码 |
| `pt` | PT 余额（可选，首次运行时会自动查询） |
| `updated_at` | 最后更新时间（同步时使用） |

### 2. 监控列表

编辑 `config.yaml`：

```yaml
comics:
  # 方式1: 直接粘贴 series URL
  - url: "https://comic-days.com/series/3269632237268447047"
    name: "雨夜の月"
    enabled: true
  
  # 方式2: 粘贴任意章节 URL（自动提取 series_id）
  - url: "https://comic-days.com/episode/2551460909710194586"
    name: "某漫画"
    enabled: true
```

## 使用

### 手动运行

```bash
# 检查所有配置的漫画
python monitor.py

# 只检查指定作品
python monitor.py --series 3269632237268447047
```

### 定时任务 (cron)

```bash
# 编辑 crontab
crontab -e

# 每 6 小时运行一次
0 */6 * * * cd /opt/comic-days-monitor && python monitor.py >> logs/cron.log 2>&1
```

## 目录结构

```
comic-days-monitor/
├── monitor.py           # 主脚本
├── sync_accounts.py     # 账号同步工具
├── config.yaml          # 配置文件
├── accounts.json        # 账号池 + PT 余额
├── requirements.txt     # Python 依赖
├── states/              # 状态文件 (自动生成)
│   └── {series_id}.json
├── downloads/           # 下载目录 (自动生成)
│   └── {series_name}/
│       └── {episode_title}/
│           └── 001.jpg
└── logs/
    └── monitor.log
```

## 状态文件

### accounts.json（全局）

```json
{
  "accounts": [
    {
      "email": "user@example.com",
      "password": "xxx",
      "pt": 50
    }
  ],
  "updated_at": "2026-03-22T15:30:00"
}
```

### states/{series_id}.json（每作品独立）

```json
{
  "series_id": "3269632237268447047",
  "last_checked": "2026-03-22T12:00:00",
  "purchased": {
    "2551460909710194586": {
      "title": "第45話",
      "mode": "ticket",
      "downloaded": 16,
      "total": 16,
      "at": "2026-03-22T12:00:00"
    }
  },
  "ticket_used": {
    "user@example.com": "2026-03-22T12:00:00"
  }
}
```

| 字段 | 说明 |
|------|------|
| `purchased` | 已下载章节（用于去重） |
| `ticket_used` | 票据使用时间（23小时冷却） |

## 下载流程

```
1. 获取章节列表 (Atom Feed)
      ↓
2. 过滤已下载章节 (检查 purchased 字段)
      ↓
3. 检查是否免费 → 是则直接下载
      ↓
4. 遍历 PT > 0 的账号:
   ├─ 复用 Session 缓存（避免频繁登录）
   ├─ 查询真实 PT 并更新 accounts.json
   ├─ 尝试票据购买
   └─ 尝试 PT 购买 → 成功则扣减 PT
      ↓
5. 原子写入 accounts.json 和 state.json
```

## 账号池同步

本地和服务器间的账号同步：

```bash
# 合并账号（基于时间戳）
python sync_accounts.py --merge-source local.json --merge-target server.json -o merged.json

# 上传到服务器
scp merged.json user@server:/opt/comic-days-monitor/accounts.json
```

**同步规则：**
- 新账号：直接添加
- 已存在账号：以 `updated_at` 时间戳更新的为准
- PT 会自动在运行时查询更新

## 技术细节

### 原子写入
所有 JSON 文件写入采用原子操作：先写 `.tmp` 文件，再重命名，防止写入过程中崩溃导致文件损坏。

### Session 缓存
账号登录后会缓存 Session，后续请求复用缓存，避免频繁登录触发防爬机制。

### PT 维护
- PT 直接存储在 `accounts.json`
- 每次购买成功后立即扣减并保存
- 下次运行时跳过 PT=0 的账号

## 注意事项

1. **Cloudflare**：服务器 IP 可能触发 CF 挑战，建议使用日本 VPS
2. **票据充能**：作品票据 23 小时充能一次
3. **单进程运行**：避免多进程同时写入 `accounts.json`

## License

MIT
