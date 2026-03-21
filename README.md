# Comic-Days 漫画监控脚本

自动监控多部漫画更新，下载最新话。

## 特点

- **纯 HTTP API**：无需浏览器，资源占用低
- **多漫画监控**：支持同时监控多部作品
- **智能购买**：票据优先，PT 补充
- **增量下载**：只下载最新话，避免重复
- **状态持久化**：断点续传，不丢进度

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
      "password": "your_password"
    }
  ]
}
```

### 2. 监控列表

编辑 `config.yaml`：

```yaml
comics:
  - series_id: "2551460909530069197"  # 从 URL 获取
    name: "モーニング"
    enabled: true
```

获取 `series_id`：访问作品页面，URL 格式为 `https://comic-days.com/series/{series_id}`

## 使用

### 手动运行

```bash
# 检查所有配置的漫画
python monitor.py

# 只检查指定作品
python monitor.py --series 2551460909530069197
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
├── config.yaml          # 配置文件
├── accounts.json        # 账号池 (不提交到 git)
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

每部作品有独立的状态文件 `states/{series_id}.json`：

```json
{
  "series_id": "2551460909530069197",
  "series_name": "モーニング",
  "last_episode_id": "2551460909530069197",
  "last_checked": "2026-03-21T12:00:00",
  "purchased": {
    "2551460909530069197": {
      "title": "第100話",
      "mode": "ticket",
      "downloaded": 20,
      "total": 20
    }
  },
  "ticket_used": {
    "email@test.com": "2026-03-21T10:00:00"
  },
  "pt_used": {
    "email@test.com": 90
  }
}
```

## 账号池同步策略

- `accounts.json` 只包含 email/password，在本地和服务器间**单向同步**
- 票据使用时间 (`ticket_used`) 和 PT 消费 (`pt_used`) 存储在各作品的 `state.json` 中
- 这样避免了本地和服务器之间的数据冲突

### 同步命令

```bash
# 本地 → 服务器 (只同步账号池)
rsync -avz accounts.json user@server:/opt/comic-days-monitor/
```

## 环境变量

```bash
# 设置基础目录 (可选)
export COMIC_DAYS_DIR=/opt/comic-days-monitor
```

## 注意事项

1. **Cloudflare**：服务器 IP 可能触发 CF 挑战，建议使用日本 VPS
2. **票据充能**：作品票据 23 小时充能一次
3. **PT 查询**：脚本运行时实时查询 PT 余额，避免同步问题

## License

MIT
