# nuist_bulletin_monitor

![GitHub code size in bytes](https://img.shields.io/github/languages/code-size/Muzyu123/nuist_bulletin_monitor) ![GitHub issues](https://img.shields.io/github/issues-raw/Muzyu123/nuist_bulletin_monitor) ![GitHub](https://img.shields.io/github/license/Muzyu123/nuist_bulletin_monitor) ![GitHub followers](https://img.shields.io/github/followers/Muzyu123?style=social) ![GitHub stars](https://img.shields.io/github/stars/Muzyu123/nuist_bulletin_monitor?style=social)

## 功能亮点
> - 适用新网站。代码构建于2026年9月
> - 不需要启动，基于Linux crontab（定时任务）保证无忧自启动和后台存活，无需每次开机后手动启动，不受用户登出、关闭终端甚至服务器重启（重启后会重新自启动）影响
> - 一键化配置，无需复杂部署
> - 极致轻量化，几乎不影响服务器性能
> - 发信失败不丢通知：命中的公告只有在邮件发送成功后才写进 `Run/state.json`；发失败就留在待发队列，下一小时重试。未命中的公告立刻记录已读，不占队列。
> - 首次运行静默播种：`Run/state.json` 不存在或为空时，只发一封「监控已启动」确认信，不会发送第一次识别到的公告刷屏。
> - 改配置下一轮生效：每次定时检查时都重新加载 `config.json` 内邮箱、关键词等配置项，无需重启监控服务。

## 写在前面
1.本意是为了帮舍友关注普通话考试报名的相关通知，顺便留意一下中秋和国庆的放假通知<br>
结果没想到这个程序第一次真正测试成功就是命中了学校通知中秋国庆连放13天的通知（南信伟大无需多言）<br>

2.运行测试环境:<br>
AlmaLinux 9.5 (Teal Serval) ：本项目依赖于 Linux 定时任务实现保活<br>
Python >= 3.8：仅使用 Python 标准库，无需引入第三方库<br>

3.本项目部分使用 Claude Code + 哦鲸鲸V4.1Flash 构建<br>
> 本程序仅作学习交流之用，禁止用于制造恶意大量访问请求、破坏计算机系统等非法用途

## 使用指南

### 前置条件

一台有 Python 3.8+ 环境的 Linux 主机<br>
有一个可以开启SMTP服务的邮箱，有一个可以自由接收邮件的邮箱

### 首次使用

1、克隆项目：

```Shell
git clone https://github.com/Muzyu123/nuist_bulletin_monitor.git
```

2、复制一份 config.example.json，重命名为 config.json，修改以下配置：

```Shell
"smtp":   { "user": "你的邮箱@xxx.com", "password": "16位授权码" },
"mail":   { "to": ["收件邮箱@xxx.com"] },
"keywords": ["示例1", "示例2", "示例3"],
```

3、验证配置是否正确，终端运行以下命令：

```Shell
cd nuist_bulletin_monitor  # 跳转到你的目录
./run.sh --check-config    # 检测配置项是否齐全
./run.sh --test-mail       # 检测SMTP是否可用/授权码是否正确
./run.sh --preview         # 检测关键词合理性，输出关键词在最新 100 条公告上命中项
```

4、正式首次启动监控服务（首次运行会把现有前 100 条公告记为已读并向目标邮箱发送「监控已启动」确认邮件）：

```Shell
./run.sh
```

5、将已启动的进程挂载为定时任务（每小时的第七分钟运行一次）：

```Shell
./install_cron.sh
```

6、确认运行状态

```Shell
./install_cron.sh --status
```

### 日常维护

```Shell
cd nuist_bulletin_monitor

./install_cron.sh --status    # 看任务状态 + 最近 10 行日志
tail -f Run/monitor.log       # 实时监控日志
./run.sh                      # 立刻运行一次查询服务
./run.sh --dry-run            # 空跑测试：只打印命中判定，不发信、不记账
./run.sh --check-config       # 校验配置
./run.sh --test-mail          # 发送测试邮件
./install_cron.sh --remove    # 取消定时任务，停止监控服务
./install_cron.sh             # 重新装载定时服务
```

### 故障排除

一、收到不到邮件

按顺序查：

```Shell
./run.sh --test-mail                   # 1. 授权码还对不对
tail -50 Run/monitor.log               # 2. 有没有「邮件发送失败」
cat Run/cron.log                       # 3. cron 到底跑没跑
grep 命中 Run/monitor.log | tail       # 4. 关键词有没有命中过
```

对照表：

| 症状                                | 原因                             | 处理                                 |
| ----------------------------------- | -------------------------------- | ------------------------------------ |
| `--test-mail` 报 `535`          | 授权码错，或用成了登录密码       | 重新生成授权码                       |
| `--test-mail` 报 `553`          | 发件地址 ≠ SMTP 登录账号        | 163 不允许代发，两者必须一致         |
| 日志没报错但没收到                  | 被投进垃圾箱了                   | 去垃圾箱找，标记为「非垃圾邮件」     |
| 日志里全是「新增 0 条」             | 正常，这段时间确实没新公告       | 不用管                               |
| 有新增但「命中 0 条」               | 关键词没覆盖到                   | `--dry-run` 看实际标题，调整关键词 |
| `Run/cron.log` 里报找不到 python | 解释器路径变了                   | 改`run.sh` 里的 `PYTHON=`        |
| `Run/state.json` 扫不到               | 关键词全空且`notify_all=false` | `--check-config` 会直接报出来      |

二、抓取失败

日志出现以下提示：

```Shell
ERROR 抓取失败，本轮不更新状态，下轮重试：...
```

抓取失败后下一小时会自动重试，如果重试顺利的话不会漏报也不会重复报。

连续多轮失败一般是：学校站点维护、网络调整、UA 被拦。持续超过一天就手动 `./run.sh` 看看具体报错。

三、重复收到同一封邮件

理论上不会（记账幂等）。若发生，检查两个 cron 任务是不是都装上了：

```Shell
crontab -l | grep -c nuist-bulletin-monitor
```

返回值多于 1 就需要运行 `./install_cron.sh --remove` 再 `./install_cron.sh` 重装。
