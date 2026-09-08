# TG 频道监控下载器(tg-channel-watch)

监听指定的 Telegram 频道,新消息里的安装包/压缩包**自动下载归档到本地**,并通过 Telegram「收藏夹」推送下载通知。7×24 后台无窗口常驻,断点续传不漏消息。

```
频道新消息 ──> Telethon(MTProto,走本机代理) ──> 规则过滤 ──> 多连接并行下载 ──> 按频道/月份归档 ──> 收藏夹通知
```

## 监控中的频道

| 频道 | 说明 |
|---|---|
| [@LwelyMods](https://t.me/LwelyMods) | Lwelyの逆向计划(逆向/魔改 App) |
| [@wsfenxiang](https://t.me/wsfenxiang) | 无双资源分享(破解/实用 App) |

## 日常使用(三个文件)

| 文件 | 作用 |
|---|---|
| `后台启动.vbs` | 双击 = 无窗口后台启动(重复双击安全,单实例锁兜底) |
| `停止监控.bat` | 双击 = 停止监控 |
| `watch.log` | 运行日志(超 5MB 自动轮转为 watch.old.log) |

- 下载位置:`downloads\频道名\年-月\`
- 通知位置:Telegram App 左上角菜单 → **收藏夹(Saved Messages)**
- 开机后不会自动运行,需要手动双击 `后台启动.vbs`(如需开机自启,建一个计划任务在登录时运行该 vbs 即可)

## 加频道 / 改规则

编辑 `config.json` 的 `watch` 列表,然后 停止监控 → 后台启动:

```json
{
  "channel": "https://t.me/xxx",     // 频道链接或 @用户名
  "enabled": true,                    // false = 暂停该频道
  "keywords": [],                     // 只下消息/文件名含关键词的(空=不过滤)
  "exclude_keywords": [],             // 排除含关键词的
  "exts": [".apk", ".exe", ".zip"],   // 只下这些扩展名
  "max_size_mb": 2048                 // 单文件大小上限
}
```

顶层还有 `"proxy"`(socks5/http,本机 127.0.0.1:7897;连接失败自动回退直连)和 `"notify_saved": true`(下载完推收藏夹)。

## 技术要点

- **凭证**:`api_id/api_hash` 用的是公开的 Telegram Desktop 客户端参数;账号登录一次后 session 存在 `session.session`,之后免登录
- **下载**:>5MB 用 [FastTelethon](https://gist.github.com/painor/7e74de80ae0c819d3e9abcf9989a8dd6) 多连接并行(小文件 1 连接,大文件最多 4 连接,保守防风控),先写 `.part` 再原子改名,限流(FloodWait)自动等待重试
- **不漏不重**:数据库 `cursors` 表记每个频道的处理进度,重启从断点增量补拉(单轮上限 500 条);实时监听先于补拉注册,重叠消息由 `files` 表唯一键 + BUSY 集合去重
- **同名文件**:不同消息的同名文件自动加消息 ID 后缀,互不覆盖
- **通知**:`client.send_message("me", ...)`,走现成 MTProto 连接,无需额外 bot

## 依赖(已装好,换机才需要)

```
G:\nixang\huanjing\python.exe -m pip install telethon cryptg "python-socks[asyncio]"
```

## 注意

- 频道文件是别人发的,安装包建议先过 [VirusTotal](https://www.virustotal.com) 再装
- 用户号跑脚本属低风险但非零风险用法:只监听、不高频拉取、不大规模下载(当前参数已保守:补拉≤500 条/次、下载≤4 连接)
- `create_tg_app.sh` 是当年 my.telegram.org 申请 api 被拒后留的一键重试脚本,与主程序无关
