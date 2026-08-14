# 天气查询插件（astrbot_plugin_weather）

一个轻量的 AstrBot 天气查询插件：支持「/天气 城市」实时天气查询，以及每天固定时间向目标群推送早间天气播报。

- 作者：云晓
- 版本：1.0.0
- 许可证：MIT（详见 LICENSE）
- 仅依赖 Python 标准库 + AstrBot 自带组件，无需安装任何第三方库

## 功能

1. **实时天气查询**
   - `/天气 北京`：查询指定城市的实时天气
   - `/天气`：查询默认城市（配置 `default_city`）或该会话最近查询过的城市
2. **早间定时播报**
   - 每天到设定时间（默认 07:30）自动向目标群推送当天天气
   - 同一群同一天只推送一次（去重记录持久化到插件数据目录）
3. **数据源**
   - 使用免费且国内可访问的 [wttr.in](https://wttr.in) JSON 接口（支持中文城市名）
   - 抓取抽象为 `_fetch_weather(city)` 方法，可自行替换；失败返回 `None` 并降级提示，不影响插件运行

## 输出示例

```
🌤️ 北京 天气预报
━━━━━━━━━━━━━━━━━━━━━━
📅 日期: 2026-08-14
☁️ 天气: 晴
🌡️ 温度: 21°C ~ 31°C
💧 湿度: 60%
🍃 风力: SE 11km/h
👕 天气炎热，建议穿短袖、短裤等清凉夏装；外出注意防晒
```

## 安装

将本插件目录 `astrbot_plugin_weather` 复制到 AstrBot 的 `data/plugins/` 目录下，重启 AstrBot 即可在插件管理页看到。

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `default_city` | string | `北京` | 「/天气」无参数时的默认查询城市 |
| `weather_push_enable` | bool | `false` | 是否启用早间定时播报 |
| `weather_push_time` | string | `07:30` | 播报触发时间（24 小时制 HH:MM） |
| `weather_push_groups` | string | （空） | 播报目标群号列表，英文逗号分隔，如 `123456789,987654321` |
| `weather_push_city` | string | （空） | 播报使用的城市，留空则使用 `default_city` |
| `weather_push_platform` | string | `onebot` | 目标群所属平台标识，须与 AstrBot 平台实例 ID 一致 |

> 提示：QQ 官方平台（qqofficial）不支持主动推送消息，请使用支持主动消息的平台（如 onebot）。

## 使用说明

1. 配置 `default_city` 后，直接发送 `/天气` 即可查询默认城市天气；
2. 查询过某个城市后，该会话下次发送 `/天气` 会优先使用最近查询过的城市；
3. 开启早间播报：设置 `weather_push_enable = true`，并在 `weather_push_groups` 填入目标群号，重启或重新加载插件后生效。

## 数据与隐私

- 最近城市记忆与推送去重记录保存在 `data/plugins/plugin_data/astrbot_plugin_weather/` 下；
- 除天气数据源外，插件不收集、不上传任何数据。

## 测试

插件自带单元测试 `test_weather.py`（mock 天气抓取、内存替身替换持久化与 context，不联网）：

```bash
D:\uv-tools\astrbot\Scripts\python.exe test_weather.py
```
