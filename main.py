# -*- coding: utf-8 -*-
"""AstrBot 天气查询插件：/天气 城市 实时天气 + 多日预报 + 生活指数 + 早间定时播报"""

import asyncio
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import MessageChain
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

# 插件元数据
PLUGIN_NAME = "astrbot_plugin_weather"
PLUGIN_AUTHOR = "云晓"
PLUGIN_DESC = "天气查询：实时天气/多日预报/生活指数/天气提醒与早间播报"
try:
    from .hub_bridge import HUB_PROTOCOL_VERSION, emit_hub_event
except ImportError:
    from hub_bridge import HUB_PROTOCOL_VERSION, emit_hub_event

PLUGIN_VERSION = "1.2.0"

# 后台定时检查间隔（秒）
PUSH_CHECK_INTERVAL = 30
# 早间播报默认时间
DEFAULT_PUSH_TIME = "07:30"

# 多日预报默认天数（wttr.in 免费源最多返回 3 天）
DEFAULT_FORECAST_DAYS = 3

# 天气数据源：优先国内可访问的 wttr.in 免费 JSON 接口（支持中文城市名），
# 按顺序逐个尝试，全部失败返回 None（降级不影响插件运行）。
# 仅使用 HTTPS 源，避免明文 HTTP 中间人篡改。
WEATHER_SOURCES = [
    "https://wttr.in/{city}?format=j1&lang=zh",
]

# 请求 UA，部分网络环境需要伪装浏览器
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 天气响应体大小上限（防止异常源导致内存膨胀）
MAX_BODY_BYTES = 2 * 1024 * 1024


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION)
class WeatherPlugin(Star):
    HUB_PROTOCOL_VERSION = HUB_PROTOCOL_VERSION
    """天气查询与早间播报：/天气 城市名 查询；定时向目标群推送当天天气"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        # 数据目录（去重记录、最近城市记忆的持久化位置）。
        # 支持通过配置项 data_dir 覆盖（测试注入使用），默认落在插件数据目录。
        custom_dir = self.config.get("data_dir")
        if custom_dir:
            self.data_dir = os.path.abspath(str(custom_dir))
        else:
            self.data_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "plugin_data",
                PLUGIN_NAME,
            )
        os.makedirs(self.data_dir, exist_ok=True)

        # 去重记录：{日期(YYYY-MM-DD): [已推送群号, ...]}
        self._pushed: dict[str, list[str]] = {}
        # 最近查询城市记忆：{会话UMO: 城市名}
        self._recent_cities: dict[str, str] = {}
        # 群号 -> 平台实例 ID 映射（从命令事件自动学习，用于定时推送定位平台）
        self._group_platforms: dict[str, str] = {}
        self._load_pushed()
        self._load_recent_cities()

        # 定时任务状态
        self._task: asyncio.Task | None = None
        self._running = False

        logger.info(f"【{PLUGIN_NAME}】插件初始化完成，定时播报: {self.config.get('weather_push_enable', False)}")

    # ========== 工具方法 ==========

    @staticmethod
    def _safe_int(value, default=None):
        """安全转 int：脏值（None/非数字/浮点字符串）回退默认值"""
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return default

    @staticmethod
    def _safe_bool(value, default: bool = False) -> bool:
        """安全转 bool：支持 bool/数字/字符串（true/false/1/0/yes/no），脏值回退默认"""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "1", "yes", "on"):
                return True
            if v in ("false", "0", "no", "off"):
                return False
        return default

    def _now(self) -> datetime:
        """当前时间（独立方法便于测试注入）"""
        return datetime.now()

    def _default_city(self) -> str:
        """读取默认城市配置（脏值防御）"""
        v = self.config.get("default_city", "")
        if isinstance(v, str) and v.strip():
            return v.strip()
        return ""

    def _recent_city(self, session_str: str) -> str:
        """读取某会话最近查询过的城市（无则返回空串）"""
        return str(self._recent_cities.get(str(session_str), "") or "").strip()

    def _remember_city(self, session_str: str, city: str):
        """记录某会话最近查询的城市并持久化"""
        session_str = str(session_str or "").strip()
        city = str(city or "").strip()
        if not session_str or not city:
            return
        self._recent_cities[session_str] = city
        self._save_recent_cities()

    def _push_enabled(self) -> bool:
        """定时播报总开关（脏值防御）"""
        return self._safe_bool(self.config.get("weather_push_enable"), False)

    def _push_time(self) -> str:
        """读取定时播报时间，校验 HH:MM 格式，非法值回退默认 07:30"""
        target = str(self.config.get("weather_push_time", DEFAULT_PUSH_TIME) or DEFAULT_PUSH_TIME).strip()
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", target):
            target = DEFAULT_PUSH_TIME
        return target

    def _push_groups(self) -> list[str]:
        """解析目标群号列表（英文逗号分隔，脏值防御）"""
        v = self.config.get("weather_push_groups", "")
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        return []

    def _learn_platform(self, event) -> None:
        """从消息事件学习「群号 -> 平台实例 ID」映射，供定时推送定位平台使用"""
        try:
            group_id = event.get_group_id()
            if not group_id:
                return
            pid = event.get_platform_id()
            if pid:
                self._group_platforms[str(group_id)] = str(pid)
        except Exception:
            # 学习失败不影响指令本身
            pass

    def _resolve_platform(self, group_id: str) -> str:
        """确定推送目标群所属平台 ID：配置优先，其次自动学习映射，再否则为空"""
        v = str(self.config.get("weather_push_platform", "") or "").strip()
        if v:
            return v
        return self._group_platforms.get(str(group_id), "")

    def _push_city(self) -> str:
        """定时播报城市：weather_push_city 优先，回退 default_city"""
        v = self.config.get("weather_push_city", "")
        if isinstance(v, str) and v.strip():
            return v.strip()
        return self._default_city()

    def _target_umo(self, group_id: str) -> str:
        """由群号构造推送目标 UMO（平台ID:GroupMessage:群号）；平台未知返回空串"""
        platform = self._resolve_platform(group_id)
        if not platform:
            return ""
        return f"{platform}:GroupMessage:{group_id}"

    # ========== 持久化 ==========

    def _pushed_file(self) -> str:
        return os.path.join(self.data_dir, "pushed.json")

    def _recent_file(self) -> str:
        return os.path.join(self.data_dir, "recent_cities.json")

    def _load_pushed(self):
        """从磁盘加载去重记录（结构校验，损坏时重置，不影响插件运行）"""
        try:
            path = self._pushed_file()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # 仅保留合法条目（日期键 + 字符串列表值）
                    self._pushed = {
                        str(k): [str(g) for g in (v if isinstance(v, list) else [])]
                        for k, v in data.items()
                    }
                else:
                    logger.warning("天气推送去重记录格式异常，已重置")
        except Exception as e:
            logger.warning(f"加载天气推送去重记录失败: {e}")

    def _save_pushed(self):
        """保存去重记录到磁盘（失败仅告警，不影响运行）"""
        try:
            with open(self._pushed_file(), "w", encoding="utf-8") as f:
                json.dump(self._pushed, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存天气推送去重记录失败: {e}")

    def _load_recent_cities(self):
        """从磁盘加载最近城市记忆（结构校验，损坏时重置）"""
        try:
            path = self._recent_file()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._recent_cities = {str(k): str(v) for k, v in data.items()}
                else:
                    logger.warning("最近城市记录格式异常，已重置")
        except Exception as e:
            logger.warning(f"加载最近城市记录失败: {e}")

    def _save_recent_cities(self):
        """保存最近城市记忆到磁盘（失败仅告警）"""
        try:
            with open(self._recent_file(), "w", encoding="utf-8") as f:
                json.dump(self._recent_cities, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存最近城市记录失败: {e}")

    def _already_pushed(self, date: str, group_id: str) -> bool:
        """同一天同一群是否已推送"""
        return group_id in self._pushed.get(date, [])

    def _mark_pushed(self, date: str, group_id: str):
        """记录某日某群已推送并持久化"""
        self._pushed.setdefault(date, [])
        if group_id not in self._pushed[date]:
            self._pushed[date].append(group_id)
        self._save_pushed()

    # ========== 天气抓取（可替换的数据源抽象） ==========

    def _fetch_raw(self, city: str):
        """获取指定城市天气源原始 JSON（轮询各数据源），全部失败返回 None。

        测试时应 mock 本方法或 _fetch_weather，不实际联网。
        """
        city = str(city or "").strip()
        if not city:
            return None
        for template in WEATHER_SOURCES:
            try:
                url = template.format(city=urllib.parse.quote(city))
                req = urllib.request.Request(url, headers={"User-Agent": _UA})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise ValueError(f"响应体过大（>{MAX_BODY_BYTES} 字节）")
                return json.loads(raw.decode("utf-8", errors="ignore"))
            except Exception as e:
                # 单个数据源失败仅告警，继续尝试下一源
                logger.warning(f"【{PLUGIN_NAME}】天气源 {template} 请求失败: {e}")
                continue
        return None

    def _forecast_days(self) -> int:
        """多日预报天数（1~7），脏值回退默认 3"""
        v = self._safe_int(self.config.get("weather_forecast_days"), DEFAULT_FORECAST_DAYS)
        if v is None:
            return DEFAULT_FORECAST_DAYS
        return min(7, max(1, v))

    def _alert_enabled(self) -> bool:
        """天气提醒开关（脏值防御），默认开启"""
        return self._safe_bool(self.config.get("weather_alert_enabled"), True)

    def _fetch_weather(self, city: str) -> dict | None:
        """获取指定城市实时天气，失败返回 None。

        依次尝试各天气数据源（优先国内可访问的 wttr.in 免费 JSON 接口），
        成功解析后返回统一结构:
            {"city", "date", "text", "temp_min", "temp_max", "humidity", "wind",
             "uv", "rain_chance"}
        全部数据源失败或解析失败返回 None（调用方降级提示，不影响插件运行）。
        测试时应 mock 本方法，不实际联网。
        """
        city = str(city or "").strip()
        if not city:
            return None
        data = self._fetch_raw(city)
        if data is None:
            return None
        return self._parse_weather_data(city, data)

    def _parse_weather_data(self, city: str, data) -> dict | None:
        """解析天气源 JSON（wttr.in j1 格式）为统一天气结构（脏值防御）"""
        try:
            if not isinstance(data, dict):
                return None
            weather_list = data.get("weather") or []
            current_list = data.get("current_condition") or []
            if not weather_list or not current_list:
                return None
            today = weather_list[0]
            cur = current_list[0]
            if not isinstance(today, dict) or not isinstance(cur, dict):
                return None
            # 天气现象（wttr.in lang=zh 时为中文描述）
            text = ""
            desc = cur.get("weatherDesc") or []
            if isinstance(desc, list) and desc and isinstance(desc[0], dict):
                text = str(desc[0].get("value") or "").strip()
            temp_min = self._safe_int(today.get("mintempC"))
            temp_max = self._safe_int(today.get("maxtempC"))
            cur_temp = self._safe_int(cur.get("temp_C"))
            # 缺少最低/最高温时回退到当前温度
            if temp_min is None:
                temp_min = cur_temp
            if temp_max is None:
                temp_max = cur_temp
            if temp_min is not None and temp_max is not None and temp_min > temp_max:
                temp_min, temp_max = temp_max, temp_min
            # 湿度（取当前湿度近似全天湿度）
            humidity = self._safe_int(cur.get("humidity"))
            # 风力（风向 + 风速）
            wind_parts = []
            dir_ = str(cur.get("winddir16Point") or "").strip()
            speed = self._safe_int(cur.get("windspeedKmph"))
            if dir_:
                wind_parts.append(dir_)
            if speed is not None:
                wind_parts.append(f"{speed}km/h")
            # 紫外线指数（当前时段）
            uv = self._safe_int(cur.get("uvIndex"))
            # 当天降雨概率（取各时段最大值）
            rain_chance = None
            hourly = today.get("hourly") or []
            chances = []
            if isinstance(hourly, list):
                for h in hourly:
                    if isinstance(h, dict):
                        c = self._safe_int(h.get("chanceofrain"))
                        if c is not None:
                            chances.append(c)
                if chances:
                    rain_chance = max(chances)
            return {
                "city": city or "未知城市",
                "date": str(today.get("date") or "").strip() or self._now().strftime("%Y-%m-%d"),
                "text": text or "未知",
                "temp_min": temp_min,
                "temp_max": temp_max,
                "humidity": humidity,
                "wind": " ".join(wind_parts),
                "uv": uv,
                "rain_chance": rain_chance,
            }
        except Exception as e:
            logger.warning(f"【{PLUGIN_NAME}】解析天气数据失败: {e}")
            return None

    # ========== 文本格式化 ==========

    def _advice(self, text: str = "", temp_min=None, temp_max=None) -> str:
        """生成穿衣/出行建议一句话（按平均温度 + 天气现象）"""
        temps = [t for t in (temp_min, temp_max) if isinstance(t, (int, float))]
        avg = sum(temps) / len(temps) if temps else None
        if avg is None:
            clothing = "请根据当地实际温度适当增减衣物"
        elif avg < 0:
            clothing = "天气严寒，注意穿厚羽绒服并做好防寒保暖"
        elif avg < 10:
            clothing = "天气寒冷，建议穿棉衣、大衣并注意保暖"
        elif avg < 20:
            clothing = "天气较凉，建议穿夹克、风衣或毛衣，早晚注意添衣"
        elif avg <= 28:
            clothing = "天气舒适，适合穿长袖或薄外套，早晚微凉可适当添衣"
        else:
            clothing = "天气炎热，建议穿短袖、短裤等清凉夏装"
        text = text or ""
        if any(k in text for k in ("雨", "雷")):
            return f"{clothing}；外出请携带雨具"
        if "雪" in text:
            return f"{clothing}；雪天路滑，出行注意交通安全"
        if "雾" in text:
            return f"{clothing}；有雾能见度低，出行请注意交通安全"
        if avg is not None and avg > 28:
            return f"{clothing}；外出注意防晒"
        return f"{clothing}；适宜出行"

    def _format_weather(self, weather: dict) -> str:
        """把统一天气结构格式化为播报文本（城市/日期/现象/温度/湿度/风力/建议）"""
        city = str(weather.get("city") or "未知城市")
        date = str(weather.get("date") or "").strip() or self._now().strftime("%Y-%m-%d")
        text = str(weather.get("text") or "未知")
        tmin = weather.get("temp_min")
        tmax = weather.get("temp_max")
        humidity = weather.get("humidity")
        wind = str(weather.get("wind") or "").strip()
        if isinstance(tmin, (int, float)) and isinstance(tmax, (int, float)):
            temp_line = f"🌡️ 温度: {int(tmin)}°C ~ {int(tmax)}°C"
        elif isinstance(tmax, (int, float)):
            temp_line = f"🌡️ 温度: {int(tmax)}°C"
        else:
            temp_line = "🌡️ 温度: 未知"
        lines = [
            f"🌤️ {city} 天气预报",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"📅 日期: {date}",
            f"☁️ 天气: {text}",
            temp_line,
        ]
        if isinstance(humidity, (int, float)):
            lines.append(f"💧 湿度: {int(humidity)}%")
        if wind:
            lines.append(f"🍃 风力: {wind}")
        lines.append(f"👕 {self._advice(text, tmin, tmax)}")
        return "\n".join(lines)

    # ========== 多日预报（v1.1.0） ==========

    def _parse_forecast_data(self, city: str, data) -> list[dict]:
        """解析天气源 JSON 为多日预报列表（wttr.in j1 格式 weather 数组）。

        每天返回统一结构:
            {"date", "label", "text", "temp_min", "temp_max", "rain_chance"}
        数据异常时尽力解析，全部失败返回空列表。
        """
        try:
            if not isinstance(data, dict):
                return []
            weather_list = data.get("weather") or []
            if not isinstance(weather_list, list):
                return []
            limit = self._forecast_days()
            days = []
            for i, day in enumerate(weather_list[:limit]):
                if not isinstance(day, dict):
                    continue
                date = str(day.get("date") or "").strip()
                tmin = self._safe_int(day.get("mintempC"))
                tmax = self._safe_int(day.get("maxtempC"))
                # 天气现象取正午时段描述，回退第一个时段
                hourly = day.get("hourly") or []
                text = ""
                rain_chance = None
                chances = []
                if isinstance(hourly, list) and hourly:
                    pick = hourly[len(hourly) // 2] if len(hourly) >= 3 else hourly[0]
                    if isinstance(pick, dict):
                        desc = pick.get("weatherDesc") or []
                        if isinstance(desc, list) and desc and isinstance(desc[0], dict):
                            text = str(desc[0].get("value") or "").strip()
                    for h in hourly:
                        if isinstance(h, dict):
                            c = self._safe_int(h.get("chanceofrain"))
                            if c is not None:
                                chances.append(c)
                if chances:
                    rain_chance = max(chances)
                label = ["今天", "明天", "后天"][i] if i < 3 else date
                days.append({
                    "date": date,
                    "label": label,
                    "text": text or "未知",
                    "temp_min": tmin,
                    "temp_max": tmax,
                    "rain_chance": rain_chance,
                })
            return [d for d in days if d["date"]]
        except Exception as e:
            logger.warning(f"【{PLUGIN_NAME}】解析多日预报失败: {e}")
            return []

    def _fetch_forecast(self, city: str) -> list[dict]:
        """抓取并解析多日预报，失败返回空列表。测试时应 mock 本方法。"""
        city = str(city or "").strip()
        if not city:
            return []
        data = self._fetch_raw(city)
        if data is None:
            return []
        return self._parse_forecast_data(city, data)

    def _format_forecast(self, city: str, days: list[dict]) -> str:
        """把多日预报格式化为播报文本（含天气提醒段落）"""
        lines = [
            f"🌤️ {city} 未来 {len(days)} 天预报",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for d in days:
            tmin, tmax = d.get("temp_min"), d.get("temp_max")
            if isinstance(tmin, (int, float)) and isinstance(tmax, (int, float)):
                temp = f"{int(tmin)}°C ~ {int(tmax)}°C"
            elif isinstance(tmax, (int, float)):
                temp = f"{int(tmax)}°C"
            else:
                temp = "未知"
            line = f"{d['label']}: {d['text']} · {temp}"
            rc = d.get("rain_chance")
            if isinstance(rc, (int, float)):
                line += f" · 降雨概率 {int(rc)}%"
            lines.append(line)
        alerts = []
        if days and self._alert_enabled():
            alerts = self._weather_alerts(days[0])
        if alerts:
            lines.append("⚠️ 天气提醒")
            for a in alerts:
                lines.append(f"  {a}")
        return "\n".join(lines)

    # ========== 生活指数与天气提醒（v1.1.0） ==========

    def _life_indices(self, weather: dict) -> list[tuple[str, str]]:
        """根据天气数据生成生活指数列表 [(名称, 说明), ...]

        包含穿衣 / 洗车 / 紫外线 / 运动四项；数据缺失项给出兜底文案。
        """
        text = str(weather.get("text") or "")
        tmin = weather.get("temp_min")
        tmax = weather.get("temp_max")
        temps = [t for t in (tmin, tmax) if isinstance(t, (int, float))]
        avg = sum(temps) / len(temps) if temps else None
        wet = any(k in text for k in ("雨", "雪"))
        # 穿衣
        clothing = self._advice(text, tmin, tmax)
        # 洗车
        rc = weather.get("rain_chance")
        if wet or (isinstance(rc, (int, float)) and rc >= 50):
            carwash = "不宜洗车：近期有雨雪，洗后易脏"
        else:
            carwash = "适宜洗车"
        # 紫外线
        uv = weather.get("uv")
        if isinstance(uv, (int, float)):
            v = float(uv)
            if v <= 2:
                uv_desc = f"{int(v)} 级（弱），外出无需特别防护"
            elif v <= 5:
                uv_desc = f"{int(v)} 级（中等），外出建议涂抹防晒霜"
            elif v <= 7:
                uv_desc = f"{int(v)} 级（强），注意防晒，建议戴帽子或打遮阳伞"
            elif v <= 10:
                uv_desc = f"{int(v)} 级（很强），避免午间长时间外出，务必防晒"
            else:
                uv_desc = f"{int(v)} 级（极强），尽量减少外出，做好全面防护"
        else:
            uv_desc = "暂无数据"
        # 运动
        bad_sport = any(k in text for k in ("雨", "雪", "雷", "雾", "沙尘"))
        if avg is None:
            sport = "温度数据缺失，请根据体感安排运动"
        elif avg > 35 or avg < -10:
            sport = "气温极端，不建议户外运动，注意防护"
        elif bad_sport:
            sport = "天气不佳，建议改为室内运动"
        else:
            sport = "适宜户外运动，运动前后注意补水"
        return [
            ("穿衣", clothing),
            ("洗车", carwash),
            ("紫外线", uv_desc),
            ("运动", sport),
        ]

    def _format_indices(self, weather: dict) -> str:
        """把生活指数格式化为播报文本（含天气提醒段落）"""
        city = str(weather.get("city") or "未知城市")
        date = str(weather.get("date") or "").strip() or self._now().strftime("%Y-%m-%d")
        lines = [
            f"🧭 {city} 生活指数",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"📅 日期: {date}",
        ]
        for name, desc in self._life_indices(weather):
            icon = {"穿衣": "👕", "洗车": "🚿", "紫外线": "🔆", "运动": "🏃"}.get(name, "•")
            lines.append(f"{icon} {name}: {desc}")
        alerts = self._weather_alerts(weather)
        if alerts:
            lines.append("⚠️ 天气提醒")
            for a in alerts:
                lines.append(f"  {a}")
        return "\n".join(lines)

    def _weather_alerts(self, day: dict) -> list[str]:
        """基于单日数据的规则化天气提醒（非官方气象预警）；开关关闭或数据为空返回空列表"""
        if not day or not self._alert_enabled():
            return []
        alerts = []
        text = str(day.get("text") or "")
        tmax = day.get("temp_max")
        tmin = day.get("temp_min")
        rc = day.get("rain_chance")
        m = re.search(r"(\d+)\s*km/h", str(day.get("wind") or ""))
        speed = int(m.group(1)) if m else None
        if isinstance(tmax, (int, float)) and tmax >= 35:
            alerts.append(f"高温提醒：最高温将达 {int(tmax)}°C，注意防暑降温、及时补水")
        if isinstance(tmin, (int, float)) and tmin <= 0:
            alerts.append(f"低温提醒：最低温降至 {int(tmin)}°C，注意防寒保暖")
        if speed is not None and speed >= 40:
            alerts.append(f"大风提醒：风速约 {speed}km/h，外出远离广告牌等高处悬挂物")
        if any(k in text for k in ("雷", "暴雨")):
            alerts.append("强对流提醒：有雷暴或强降雨，请减少外出、远离积水与大树")
        elif "雾" in text:
            alerts.append("大雾提醒：能见度较低，驾车请减速慢行")
        if not any(k in text for k in ("雨", "雷")) and isinstance(rc, (int, float)) and rc >= 80:
            alerts.append(f"降雨提醒：降雨概率约 {int(rc)}%，出门请携带雨具")
        return alerts

    # ========== 发送 ==========

    async def _send_text_to(self, umo: str, text: str) -> bool:
        """向指定 UMO 会话推送纯文本；成功返回 True，失败告警并返回 False（调用方不记账）"""
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
            return True
        except Exception as e:
            logger.warning(f"【{PLUGIN_NAME}】推送天气到 {umo} 失败: {e}")
            return False

    def _send_text(self, event: AstrMessageEvent, text: str):
        """构造命令回复的纯文本结果"""
        return event.chain_result([Plain(text)])

    # ========== 定时播报 ==========

    async def initialize(self) -> None:
        """插件加载/重载时启动定时播报任务"""
        await self._start_push_loop()

    async def _start_push_loop(self):
        """启动定时播报任务（幂等：重复调用不会重复启动）"""
        if not self._push_enabled():
            return
        if self._running:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._running = True
        self._task = asyncio.create_task(self._push_loop())
        self._task.add_done_callback(
            lambda t: (
                logger.error(f"定时播报任务异常退出: {t.exception()}")
                if not t.cancelled() and t.exception()
                else None
            )
        )
        logger.info(f"【{PLUGIN_NAME}】定时播报已启动，每日 {self._push_time()} 推送")

    async def _push_loop(self):
        """后台循环：每 30 秒检查一次，到达设定时间向目标群推送（同天同群去重）"""
        while self._running:
            try:
                await self._check_and_push(self._now())
            except Exception as e:
                # 单次检查异常不影响后续轮询
                logger.warning(f"【{PLUGIN_NAME}】定时播报检查异常: {e}")
            await asyncio.sleep(PUSH_CHECK_INTERVAL)

    async def _check_and_push(self, now: datetime):
        """单次定时检查：运行中 && 启用 && 到达时间 && 有目标群 && 同天同群未推送 时推送"""
        if not self._running:
            return
        if not self._push_enabled():
            return
        target = self._push_time()
        cur = now.strftime("%H:%M")
        if cur < target:
            return
        today = now.strftime("%Y-%m-%d")
        groups = self._push_groups()
        if not groups:
            return
        city = self._push_city()
        if not city:
            logger.warning(f"【{PLUGIN_NAME}】未配置播报城市，本次跳过推送")
            return
        # 抓取失败返回 None 则本次不推送（降级，不影响插件运行）；同步网络请求放入线程池避免阻塞事件循环
        weather = await asyncio.to_thread(self._fetch_weather, city)
        if not weather:
            logger.warning(f"【{PLUGIN_NAME}】{today} 天气抓取失败，本次跳过推送")
            return
        text = self._format_weather(weather)
        for group in groups:
            if self._already_pushed(today, group):
                continue
            umo = self._target_umo(group)
            if not umo:
                logger.warning(
                    f"【{PLUGIN_NAME}】群 {group} 的平台 ID 未知"
                    f"（可在 weather_push_platform 中指定，或先在该群使用一次 /天气），本次跳过推送"
                )
                continue
            if await self._send_text_to(umo, text):
                self._mark_pushed(today, group)
                alerts = self._weather_alerts(weather) if hasattr(self, "_weather_alerts") else []
                ev_name = "weather.alert" if alerts else "weather.pushed"
                emit_hub_event(PLUGIN_NAME, ev_name, {"umo": umo, "group_id": group, "city": city, "alerts": alerts, "summary": text[:280]}, self.context)

    # ========== 指令处理 ==========

    @filter.command("天气")
    async def weather_cmd(self, event: AstrMessageEvent):
        """/天气 [城市] 实时天气；/天气 预报 [城市] 多日预报；/天气 指数 [城市] 生活指数"""
        try:
            self._learn_platform(event)
            raw = event.message_str.strip()
            if raw.startswith("/"):
                raw = raw[1:]
            city = ""
            m = re.match(r"^天气\s*(.*)$", raw, re.S)
            rest = (m.group(1) or "").strip() if m else ""
            # 子命令识别：预报/未来 → 多日预报；指数 → 生活指数
            sub = ""
            for kw in ("预报", "未来", "指数"):
                if rest.startswith(kw):
                    sub = kw
                    rest = rest[len(kw):].strip()
                    break
            city = rest.split()[0] if rest.split() else rest
            # 城市参数取第一段（/天气 北京 上海 只查询第一个）
            if not city:
                city = self._default_city() or self._recent_city(str(event.session))
            if not city:
                return self._send_text(
                    event,
                    "❌ 未设置默认城市，请发送「/天气 城市名」查询，或在插件配置中设置 default_city",
                )
            display_city = city
            if sub == "指数":
                return await self._cmd_indices(event, display_city)
            if sub in ("预报", "未来"):
                return await self._cmd_forecast(event, display_city)
            return await self._cmd_current(event, display_city)
        except Exception as e:
            logger.error(f"【{PLUGIN_NAME}】天气查询异常: {e}")
            return self._send_text(event, f"❌ 查询天气失败: {str(e)}")

    async def _cmd_current(self, event: AstrMessageEvent, city: str):
        """实时天气查询"""
        weather = await asyncio.to_thread(self._fetch_weather, city)
        if not weather:
            return self._send_text(event, f"❌ 获取「{city}」天气失败，请检查城市名是否正确或稍后重试")
        # 查询成功后记录最近城市（按会话）并持久化
        self._remember_city(str(event.session), weather.get("city") or city)
        return self._send_text(event, self._format_weather(weather))

    async def _cmd_forecast(self, event: AstrMessageEvent, city: str):
        """多日预报查询（默认 3 天，可配置 1~7 天）"""
        days = await asyncio.to_thread(self._fetch_forecast, city)
        if not days:
            return self._send_text(event, f"❌ 获取「{city}」多日预报失败，请检查城市名是否正确或稍后重试")
        # 预报成功同样记忆最近城市
        self._remember_city(str(event.session), city)
        return self._send_text(event, self._format_forecast(city, days))

    async def _cmd_indices(self, event: AstrMessageEvent, city: str):
        """生活指数查询（穿衣/洗车/紫外线/运动 + 天气提醒）"""
        weather = await asyncio.to_thread(self._fetch_weather, city)
        if not weather:
            return self._send_text(event, f"❌ 获取「{city}」天气失败，无法生成生活指数，请稍后重试")
        self._remember_city(str(event.session), weather.get("city") or city)
        return self._send_text(event, self._format_indices(weather))

    # ========== 生命周期 ==========

    async def terminate(self):
        """插件卸载时取消定时任务"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def hub_status(self) -> dict:
        return {
            "version": PLUGIN_VERSION,
            "status": "online",
            "metrics": {"默认城市": self._default_city() or "未设", "早间播报": "开" if self._push_enabled() else "关", "目标群": len(self._push_groups())},
            "actions": ["get_status", "query_city"],
        }

    async def hub_action(self, action: str, params: dict | None = None) -> dict:
        p = params or {}
        if action == "get_status":
            return {"ok": True, "data": {"default_city": self._default_city(), "push_enabled": self._push_enabled(), "push_time": self._push_time(), "push_groups": self._push_groups()}}
        if action == "query_city":
            city = str(p.get("city") or self._default_city() or "北京").strip()
            w = await asyncio.to_thread(self._fetch_weather, city)
            if not w:
                return {"ok": False, "error": f"获取 {city} 天气失败"}
            return {"ok": True, "data": {"city": city, "text": self._format_weather(w)}}
        return {"ok": False, "error": f"不支持的动作：{action}"}
