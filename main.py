# -*- coding: utf-8 -*-
"""AstrBot 天气查询插件：/天气 城市 实时天气查询 + 早间定时播报"""

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
PLUGIN_DESC = "天气查询与早间播报"
PLUGIN_VERSION = "1.0.0"

# 后台定时检查间隔（秒）
PUSH_CHECK_INTERVAL = 30
# 早间播报默认时间
DEFAULT_PUSH_TIME = "07:30"

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

    def _fetch_weather(self, city: str) -> dict | None:
        """获取指定城市实时天气，失败返回 None。

        依次尝试各天气数据源（优先国内可访问的 wttr.in 免费 JSON 接口），
        成功解析后返回统一结构:
            {"city", "date", "text", "temp_min", "temp_max", "humidity", "wind"}
        全部数据源失败或解析失败返回 None（调用方降级提示，不影响插件运行）。
        测试时应 mock 本方法，不实际联网。
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
                data = json.loads(raw.decode("utf-8", errors="ignore"))
                parsed = self._parse_weather_data(city, data)
                if parsed:
                    return parsed
            except Exception as e:
                # 单个数据源失败仅告警，继续尝试下一源
                logger.warning(f"【{PLUGIN_NAME}】天气源 {template} 请求失败: {e}")
                continue
        return None

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
            return {
                "city": city or "未知城市",
                "date": str(today.get("date") or "").strip() or self._now().strftime("%Y-%m-%d"),
                "text": text or "未知",
                "temp_min": temp_min,
                "temp_max": temp_max,
                "humidity": humidity,
                "wind": " ".join(wind_parts),
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

    @filter.on_astrbot_loaded()
    async def _start_push_loop(self):
        """启动定时播报任务（幂等：重复调用不会重复启动）"""
        if not self._push_enabled():
            return
        if self._running:
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

    # ========== 指令处理 ==========

    @filter.command("天气")
    async def weather_cmd(self, event: AstrMessageEvent):
        """/天气 城市名 查询实时天气；/天气 查询默认城市或最近查询过的城市"""
        try:
            self._learn_platform(event)
            raw = event.message_str.strip()
            if raw.startswith("/"):
                raw = raw[1:]
            city = ""
            m = re.match(r"^天气\s*(.*)$", raw, re.S)
            if m:
                city = (m.group(1) or "").strip()
            # 城市参数取第一段（/天气 北京 上海 只查询第一个）
            if city:
                city = city.split()[0] if city.split() else city
            if not city:
                city = self._default_city() or self._recent_city(str(event.session))
            if not city:
                return self._send_text(event, "❌ 未设置默认城市，请发送「/天气 城市名」查询，或在插件配置中设置 default_city")
            weather = await asyncio.to_thread(self._fetch_weather, city)
            if not weather:
                return self._send_text(event, f"❌ 获取「{city}」天气失败，请检查城市名是否正确或稍后重试")
            # 查询成功后记录最近城市（按会话）并持久化
            self._remember_city(str(event.session), weather.get("city") or city)
            return self._send_text(event, self._format_weather(weather))
        except Exception as e:
            logger.error(f"【{PLUGIN_NAME}】天气查询异常: {e}")
            return self._send_text(event, f"❌ 查询天气失败: {str(e)}")

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
