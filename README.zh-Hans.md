# Jev Autopilot

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Tests](https://github.com/davidzha712/ha-jev-autopilot/actions/workflows/tests.yaml/badge.svg)](https://github.com/davidzha712/ha-jev-autopilot/actions/workflows/tests.yaml)

[English](README.md)

一个 Home Assistant 集成：让 [TypeSafe Jev](https://typesafe.ai) 判断每个房间此刻需要什么，
再用普通的服务调用去执行。

Jev 是一个"System One"决策模型：读一段对现状的描述，对是非题、选择题、打分题给出校准过的概率。
它不聊天，也不生成文本。Jev Autopilot 负责出题（"吸顶灯现在该开吗？""哪个亮度合适？"），
每个房间一次批量调用，再用可审查的确定性规则把概率变成动作。

**模型只给建议，代码负责执行。** 阈值、冷却、手动覆盖识别、确认层全部是带测试的普通 Python。

> 想把 Jev 当作传感器、对话代理和动作，自己接进自动化？请用
> [HA-Jev](https://github.com/AboveColin/HA-Jev)。Jev Autopilot 是基于同一 API 的
> "判断并执行"层。

## 功能

- **以房间为单位。** 添加房间，选它的灯、开关、风扇、温控器、媒体播放器，以及要参考的传感器。
- **灯：** 开关、亮度、色温，档位由你自定义。
- **开关和风扇：** 开关。**温控器：** 选一个加热档位。**媒体播放器：** 没人看时关掉，从不打开。
- **先问再做。** 门锁、电脑插座、电饭煲、摄像头开关：放进"先问我"的设备只会以带
  **执行**/**跳过** 按钮的通知推到手机。门锁只会被建议上锁，永远不会被建议开锁。
- **尊重手动操作。** 人做的改动（墙面开关、App、其他自动化）通过 context 识别，
  在预设的保持时间内不再动它。
- **干净交还。** 你为房间列出的现有自动化，在自动驾驶接管期间关闭；在你关掉自动驾驶、
  Jev 连续 3 次不可达、当日预算用完、或 API key 被拒时重新打开。
- **隐私。** 发给 Jev 的文本包含设备名、状态和传感器数值，住户只以人数出现（"3 人中 2 人在家"）。
  发送前，设备名、房间名、传感器状态、问题文本和备注里：
  - Home Assistant 已知的 entity id（包括已禁用的）替换成 "a device"；
  - 住户和 Home Assistant 用户的姓名替换成 "a resident"，不区分大小写：全名、其中两个字符及以上的单词，
    以及中文名的名字部分（王小明里的"小明"）。名字后面跟所有格或数字也能识别（"Annas Lampe"、"Bob2"）；
  - IPv4 地址，连同包住它的更长的点分数字串，替换成 "[address]"。

  和其他字母连写的名字（"BobPC"）识别不了。其他你写进名称或备注的个人信息会原样发送。没有 friendly name 的设备，会以 Home Assistant
  从 entity id 推出的名字发送。

## 安装

### HACS（自定义仓库）

1. HACS → ⋮ → *Custom repositories* → 添加
   `https://github.com/davidzha712/ha-jev-autopilot`，类型 *Integration*。
2. 安装 **Jev Autopilot**，重启 Home Assistant。

### 手动

把 `custom_components/jev_autopilot` 复制到 `config/custom_components/`，重启。

需要 Home Assistant 2026.9 或更新版本。

## 配置

*设置 → 设备与服务 → 添加集成 → Jev Autopilot。*

| 字段 | TypeSafe 直连 | OpenRouter |
|---|---|---|
| API key | TypeSafe key | OpenRouter key |
| Base URL | `https://api.typesafe.ai` | `https://openrouter.ai/api` |
| Model | `jev-latest` | `typesafe/jev-1.13` |

保存前会花一次极小的调用验证 key 和地址。

然后在集成卡片上点 **添加房间**：

| 字段 | 含义 |
|---|---|
| 灯、开关、风扇、温控器、媒体播放器 | 直接控制 |
| 先问我 | 门锁和有风险的设备，只推送建议 |
| 环境传感器 | 人体、占用、照度、温度、门窗。变化会触发重新判断 |
| 要替换的自动化 | 房间被接管期间关闭 |
| 房间备注 | 用大白话写习惯，如"18 点前办公，之后要温馨的光" |

一个设备只能属于一个房间。

## 选项

集成卡片上点 *配置*。

| 选项 | 默认 | 说明 |
|---|---|---|
| 默认预设 | balanced | 没单独选过档位的房间都用它；房间的档位选择器优先 |
| 确认通知发到 | — | mobile_app 的 notify 服务 |
| 确认阈值 | 0.90 | 下限 0.80。低于它连建议都不发 |
| 每日调用预算 | 3000 | 用完后房间暂停并交还自动化，午夜重置 |
| 巡检间隔 | 10 分钟 | 下限 2 分钟。传感器变化也会触发（30 秒去抖） |
| 亮度档位 (%) | `10, 30, 50, 75, 100` | Jev 在这个刻度上选点 |
| 色温档位 (K) | `2700, 3500, 4000` | 暖到冷 |
| 加热档位 (°C) | `off, 17, 20, 22` | 第一个值可以是 `off` |
| 全屋备注 | — | 全屋习惯，每次调用都会带上 |

### 预设

| 预设 | 开的阈值 | 关的阈值 | 改动后冷却 | 手动改动后保持 |
|---|---|---|---|---|
| conservative | p ≥ 0.85 | p ≤ 0.15 | 15 分钟 | 60 分钟 |
| balanced | p ≥ 0.75 | p ≤ 0.25 | 5 分钟 | 30 分钟 |
| aggressive | p ≥ 0.65 | p ≤ 0.35 | 2 分钟 | 15 分钟 |

两个阈值之间不做改动，Jev 拿不准时灯不会闪。

## 实体

每个房间一个设备：

| 实体 | 作用 |
|---|---|
| `switch.<房间>_autopilot` | 开关该房间的自动驾驶 |
| `select.<房间>_preset` | conservative / balanced / aggressive |
| `sensor.<房间>_last_decision` | 做了什么、为什么；属性里有动作、Jev 的概率、延迟和 token |
| `sensor.<房间>_status` | ok / disabled / degraded / paused |
| `sensor.<房间>_manual_overrides_today` | 今天被人手动推翻的次数，用来调参 |

全局设备 *Jev Autopilot*：`sensor.jev_autopilot_calls_today`、`sensor.jev_autopilot_cost_today`（美元）。

## 费用

Jev 只按输入 token 收费，输出免费。一次房间调用通常一两千输入 token。按 OpenRouter
2026 年 9 月价格每百万输入 token $0.042，七个房间每 10 分钟巡检一次，每月约 $2–3。
以服务商当前价格为准；费用传感器用的是 API 返回的 token 数。

## 排障

- **状态 `degraded`：** Jev 连续失败 3 次。修复中心会有一条写明错误的问题。该房间的自动化
  已经恢复；下次调用成功后自动回到接管状态。
- **状态 `paused`：** 当日预算用完。在选项里调高，或等到午夜。
- **手机收不到：** 确认"确认通知发到"选的是 `mobile_app` 服务。建议 30 分钟后失效；
  点"跳过"后同一建议 2 小时内不再发。
- **灯总被改回去：** 换更保守的预设、写房间备注，或看 `sensor.<房间>_last_decision` 的属性，
  了解 Jev 的回答。
- **诊断：** 集成的"下载诊断"包含选项、房间、计数和最近 50 条决策，API key 已脱敏。

## 已知限制

- Jev 只能看到传感器报告的内容。没有人体或占用传感器的房间，主要靠时间和设备状态判断。
- 启动时不调用 API；被吊销的 key 会在第一次房间判断时发现并发起重新认证。
- 只控制亮度和色温，不控制颜色。
- 设备在指令很久之后才回报新状态（慢速渐变的灯、云端轮询的温控阀）时，可能被当成手动修改，
  按档位的手动保持时间锁住。
- 删除集成会同时删除保存的决策日志。

## 卸载

在 *设置 → 设备与服务* 删除集成。卸载时会重新打开它关掉的自动化。删除单个房间同理。
删除集成时若某个自动化无法重新打开（已不存在或调用失败），会发一条通知列出它，请手动打开。

## 开发

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-test.txt ruff mypy
.venv/bin/pytest --cov=custom_components.jev_autopilot
.venv/bin/ruff check custom_components tests && .venv/bin/ruff format --check custom_components tests
.venv/bin/mypy --strict --ignore-missing-imports custom_components/jev_autopilot
```

设计文档在 [docs/superpowers/specs](docs/superpowers/specs)。

## 免责声明

与 TypeSafe 无关联。Jev 是其所有者的商标。家里发生什么由你负责：凡是判断错了会有后果的设备，
请放进"先问我"。

## 许可证

MIT
