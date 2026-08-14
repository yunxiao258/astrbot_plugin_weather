# -*- coding: utf-8 -*-
"""天气查询插件单元测试：命令查询、格式化、默认城市/最近城市记忆、定时播报、去重、脏值防御

所有测试均 mock 天气抓取（_fetch_weather），使用内存替身替换持久化与 context，
不联网、不写真实 plugin_data。
"""
import asyncio
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, r"D:\astrbot\data\plugins")

from astrbot_plugin_weather.main import WeatherPlugin  # noqa: E402

# 固定天气数据（模拟 _fetch_weather 返回的统一结构）
FIXED_WEATHER = {
    "city": "北京",
    "date": "2026-08-14",
    "text": "晴",
    "temp_min": 21,
    "temp_max": 31,
    "humidity": 60,
    "wind": "SE 11km/h",
}


class FakeSession:
    """最小会话替身"""
    def __init__(self, umo="onebot:GroupMessage:123"):
        self.umo = umo

    def __str__(self):
        return self.umo


class FakeEvent:
    """最小事件替身：仅支持 session/message_str/chain_result/get_sender_id"""
    def __init__(self, message_str="/天气 北京", umo="onebot:GroupMessage:123"):
        self.session = FakeSession(umo)
        self.message_str = message_str

    def get_sender_id(self):
        return "10001"

    def chain_result(self, chain):
        return chain


class FakeContext:
    """内存替身：记录 send_message 调用，不真实发送"""
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True


def make_plugin(config=None, context=None):
    """构造插件实例：通过 data_dir 配置把持久化重定向到临时目录，不写真实 plugin_data"""
    tmp = tempfile.mkdtemp(prefix="weather_test_")
    cfg = dict(config or {})
    cfg["data_dir"] = tmp
    return WeatherPlugin(context or FakeContext(), cfg)


def chain_text(chain) -> str:
    """从回复（FakeEvent 返回 list / 推送的 MessageChain）中提取纯文本"""
    return getattr(chain, "chain", chain)[0].text


class TestFormatWeather(unittest.TestCase):
    """天气文本格式化：城市/日期/现象/温度/湿度/风力/建议"""

    def test_format_contains_city_and_temp(self):
        text = make_plugin()._format_weather(dict(FIXED_WEATHER))
        self.assertIn("北京", text)
        self.assertIn("2026-08-14", text)
        self.assertIn("晴", text)
        self.assertIn("21°C ~ 31°C", text)
        self.assertIn("湿度: 60%", text)
        self.assertIn("风力: SE 11km/h", text)
        self.assertIn("舒适", text)

    def test_format_without_humidity_and_wind(self):
        w = dict(FIXED_WEATHER, humidity=None, wind="", text="多云")
        text = make_plugin()._format_weather(w)
        self.assertIn("上海" if False else "北京", text)
        self.assertIn("多云", text)
        self.assertNotIn("湿度", text)
        self.assertNotIn("风力", text)

    def test_format_no_temp(self):
        w = {"city": "北京", "date": "2026-08-14", "text": "阴",
             "temp_min": None, "temp_max": None, "humidity": None, "wind": ""}
        text = make_plugin()._format_weather(w)
        self.assertIn("温度: 未知", text)
        self.assertIn("增减衣物", text)


class TestAdvice(unittest.TestCase):
    """穿衣/出行建议"""

    def test_hot(self):
        self.assertIn("炎热", make_plugin()._advice("晴", 28, 36))

    def test_cold(self):
        self.assertIn("寒冷", make_plugin()._advice("晴", -2, 8))

    def test_frigid(self):
        self.assertIn("严寒", make_plugin()._advice("晴", -12, -4))

    def test_cool(self):
        self.assertIn("夹克", make_plugin()._advice("晴", 10, 18))

    def test_mild(self):
        self.assertIn("舒适", make_plugin()._advice("晴", 20, 26))

    def test_rain(self):
        self.assertIn("雨具", make_plugin()._advice("小雨", 15, 20))

    def test_snow(self):
        self.assertIn("路滑", make_plugin()._advice("小雪", -2, 1))

    def test_fog(self):
        self.assertIn("能见度", make_plugin()._advice("雾", 12, 18))

    def test_no_temp(self):
        self.assertIn("增减衣物", make_plugin()._advice("晴", None, None))


class TestParseWeatherData(unittest.TestCase):
    """天气源 JSON 解析（wttr.in j1 格式，脏值防御）"""

    def _wttr_data(self, **overrides):
        data = {
            "current_condition": [{
                "temp_C": "25", "humidity": "60",
                "weatherDesc": [{"value": "晴"}],
                "winddir16Point": "SE", "windspeedKmph": "11",
            }],
            "weather": [{"date": "2026-08-14", "mintempC": "21", "maxtempC": "31"}],
        }
        data.update(overrides)
        return data

    def test_parse_valid(self):
        w = make_plugin()._parse_weather_data("北京", self._wttr_data())
        self.assertIsNotNone(w)
        self.assertEqual(w["city"], "北京")
        self.assertEqual(w["text"], "晴")
        self.assertEqual(w["temp_min"], 21)
        self.assertEqual(w["temp_max"], 31)
        self.assertEqual(w["humidity"], 60)
        self.assertIn("SE", w["wind"])
        self.assertIn("11km/h", w["wind"])

    def test_parse_invalid_shapes(self):
        p = make_plugin()
        self.assertIsNone(p._parse_weather_data("北京", None))
        self.assertIsNone(p._parse_weather_data("北京", []))
        self.assertIsNone(p._parse_weather_data("北京", {"foo": 1}))
        self.assertIsNone(p._parse_weather_data("北京", {"weather": [], "current_condition": []}))

    def test_parse_missing_temp_falls_back_to_current(self):
        data = self._wttr_data()
        del data["weather"][0]["mintempC"]
        del data["weather"][0]["maxtempC"]
        w = make_plugin()._parse_weather_data("上海", data)
        self.assertEqual(w["temp_min"], 25)
        self.assertEqual(w["temp_max"], 25)

    def test_parse_temp_swapped(self):
        # 最高温低于最低温（脏数据）时自动交换
        data = self._wttr_data()
        data["weather"][0]["mintempC"] = "31"
        data["weather"][0]["maxtempC"] = "21"
        w = make_plugin()._parse_weather_data("北京", data)
        self.assertEqual(w["temp_min"], 21)
        self.assertEqual(w["temp_max"], 31)

    def test_parse_float_strings(self):
        data = self._wttr_data()
        data["weather"][0]["mintempC"] = "21.0"
        data["weather"][0]["maxtempC"] = "31.5"
        data["current_condition"][0]["temp_C"] = "25.2"
        w = make_plugin()._parse_weather_data("北京", data)
        self.assertEqual(w["temp_min"], 21)
        self.assertEqual(w["temp_max"], 31)


class TestQueryCommand(unittest.TestCase):
    """/天气 命令：查询、未知城市、失败降级、默认城市回退、最近城市记忆"""

    def run_cmd(self, plugin, event):
        return asyncio.run(plugin.weather_cmd(event))

    def test_query_with_city(self):
        p = make_plugin()
        p._fetch_weather = lambda city: dict(FIXED_WEATHER)
        result = self.run_cmd(p, FakeEvent("/天气 北京"))
        text = chain_text(result)
        self.assertIn("北京", text)
        self.assertIn("21°C ~ 31°C", text)

    def test_query_city_without_slash(self):
        p = make_plugin()
        p._fetch_weather = lambda city: dict(FIXED_WEATHER, city=city)
        result = self.run_cmd(p, FakeEvent("天气 广州"))
        self.assertIn("广州", chain_text(result))

    def test_unknown_city_prompt(self):
        # 未知城市：数据源无返回 → 降级提示
        p = make_plugin()
        p._fetch_weather = lambda city: None
        result = self.run_cmd(p, FakeEvent("/天气 不存在的地方"))
        text = chain_text(result)
        self.assertIn("不存在的地方", text)
        self.assertIn("失败", text)

    def test_fetch_failure_degrades(self):
        # 抓取抛异常 → 降级提示，不崩溃
        p = make_plugin()

        def boom(city):  # noqa: ARG001
            raise RuntimeError("network down")

        p._fetch_weather = boom
        result = self.run_cmd(p, FakeEvent("/天气 北京"))
        self.assertIn("失败", chain_text(result))

    def test_default_city_fallback(self):
        # 无参数时回退到配置 default_city
        p = make_plugin(config={"default_city": "广州"})
        p._fetch_weather = lambda city: dict(FIXED_WEATHER, city=city)
        result = self.run_cmd(p, FakeEvent("/天气"))
        self.assertIn("广州", chain_text(result))

    def test_no_default_city_hint(self):
        p = make_plugin(config={"default_city": ""})
        result = self.run_cmd(p, FakeEvent("/天气"))
        self.assertIn("默认城市", chain_text(result))

    def test_recent_city_remembered(self):
        # 查过上海后，同会话无参数查询自动使用上海
        p = make_plugin(config={"default_city": ""})
        p._fetch_weather = lambda city: dict(FIXED_WEATHER, city=city)
        self.run_cmd(p, FakeEvent("/天气 上海"))
        result = self.run_cmd(p, FakeEvent("/天气"))
        self.assertIn("上海", chain_text(result))

    def test_recent_city_not_shared_between_sessions(self):
        p = make_plugin(config={"default_city": ""})
        p._fetch_weather = lambda city: dict(FIXED_WEATHER, city=city)
        self.run_cmd(p, FakeEvent("/天气 上海", umo="onebot:GroupMessage:111"))
        result = self.run_cmd(p, FakeEvent("/天气", umo="onebot:GroupMessage:222"))
        self.assertIn("默认城市", chain_text(result))

    def test_recent_city_persisted(self):
        # 新实例从磁盘恢复最近城市记忆（同一 data_dir）
        p = make_plugin(config={"default_city": ""})
        p._fetch_weather = lambda city: dict(FIXED_WEATHER, city=city)
        self.run_cmd(p, FakeEvent("/天气 杭州", umo="onebot:GroupMessage:1"))
        p2 = WeatherPlugin(FakeContext(), {"data_dir": p.data_dir})
        self.assertEqual(p2._recent_cities.get("onebot:GroupMessage:1"), "杭州")

    def test_default_city_prefers_over_recent(self):
        # default_city 优先于最近城市记忆
        p = make_plugin(config={"default_city": "北京"})
        p._fetch_weather = lambda city: dict(FIXED_WEATHER, city=city)
        self.run_cmd(p, FakeEvent("/天气 上海"))
        result = self.run_cmd(p, FakeEvent("/天气"))
        self.assertIn("北京", chain_text(result))


class TestPushSchedule(unittest.TestCase):
    """定时播报：到达时间触发、同天去重、关闭/未运行/未到点不推"""

    def make_push_plugin(self, config=None):
        p = make_plugin(config={
            "weather_push_enable": True,
            "weather_push_time": "07:30",
            "weather_push_groups": "123,456",
            "weather_push_platform": "onebot",
            "default_city": "北京",
            **(config or {}),
        })
        p._fetch_weather = lambda city: dict(FIXED_WEATHER)
        return p

    def test_push_at_time(self):
        p = self.make_push_plugin()
        p._running = True
        asyncio.run(p._check_and_push(datetime(2026, 8, 14, 7, 30, 0)))
        ctx = p.context
        self.assertEqual(len(ctx.sent), 2)  # 两个目标群各一次
        umos = {umo for umo, _ in ctx.sent}
        self.assertEqual(umos, {"onebot:GroupMessage:123", "onebot:GroupMessage:456"})
        text = chain_text(ctx.sent[0][1])
        self.assertIn("北京", text)
        self.assertIn("晴", text)

    def test_push_same_day_dedup(self):
        p = self.make_push_plugin()
        p._running = True
        now = datetime(2026, 8, 14, 7, 31, 0)
        asyncio.run(p._check_and_push(now))
        asyncio.run(p._check_and_push(now))  # 同日再次检查不重复推送
        self.assertEqual(len(p.context.sent), 2)

    def test_push_new_day_again(self):
        p = self.make_push_plugin()
        p._running = True
        asyncio.run(p._check_and_push(datetime(2026, 8, 14, 7, 30)))
        asyncio.run(p._check_and_push(datetime(2026, 8, 15, 7, 30)))
        self.assertEqual(len(p.context.sent), 4)

    def test_push_dedup_persisted(self):
        # 去重记录落盘：新实例（同一 data_dir）不会重复推送
        p = self.make_push_plugin()
        p._running = True
        asyncio.run(p._check_and_push(datetime(2026, 8, 14, 7, 30)))
        p2 = self.make_push_plugin()
        p2.data_dir = p.data_dir
        p2._load_pushed()
        p2._running = True
        asyncio.run(p2._check_and_push(datetime(2026, 8, 14, 7, 30)))
        self.assertEqual(len(p2.context.sent), 0)

    def test_push_disabled_no_send(self):
        p = self.make_push_plugin()
        p._running = True
        p.config["weather_push_enable"] = False
        asyncio.run(p._check_and_push(datetime(2026, 8, 14, 7, 30)))
        self.assertEqual(p.context.sent, [])

    def test_push_not_running_no_send(self):
        p = self.make_push_plugin()
        p._running = False
        asyncio.run(p._check_and_push(datetime(2026, 8, 14, 7, 30)))
        self.assertEqual(p.context.sent, [])

    def test_push_before_time_no_send(self):
        p = self.make_push_plugin()
        p._running = True
        asyncio.run(p._check_and_push(datetime(2026, 8, 14, 7, 29, 59)))
        self.assertEqual(p.context.sent, [])

    def test_push_fetch_failure_skips(self):
        # 抓取失败（None）→ 本次跳过，不推送
        p = self.make_push_plugin()
        p._running = True
        p._fetch_weather = lambda city: None
        asyncio.run(p._check_and_push(datetime(2026, 8, 14, 7, 30)))
        self.assertEqual(p.context.sent, [])

    def test_push_no_groups_no_send(self):
        p = self.make_push_plugin(config={"weather_push_groups": ""})
        p._running = True
        asyncio.run(p._check_and_push(datetime(2026, 8, 14, 7, 30)))
        self.assertEqual(p.context.sent, [])

    def test_push_no_city_no_send(self):
        p = self.make_push_plugin(config={"weather_push_city": "", "default_city": ""})
        p._running = True
        asyncio.run(p._check_and_push(datetime(2026, 8, 14, 7, 30)))
        self.assertEqual(p.context.sent, [])

    def test_push_city_prefers_push_city(self):
        p = self.make_push_plugin(config={"weather_push_city": "深圳"})
        p._running = True
        p._fetch_weather = lambda city: dict(FIXED_WEATHER, city=city)
        asyncio.run(p._check_and_push(datetime(2026, 8, 14, 7, 30)))
        self.assertIn("深圳", chain_text(p.context.sent[0][1]))

    def test_push_time_dirty_config_still_fires(self):
        # 脏的播报时间回退默认 07:30，07:31 仍然触发
        p = self.make_push_plugin(config={"weather_push_time": "not-a-time"})
        p._running = True
        asyncio.run(p._check_and_push(datetime(2026, 8, 14, 7, 31, 0)))
        self.assertEqual(len(p.context.sent), 2)


class TestLoopLifecycle(unittest.TestCase):
    """加载钩子与 terminate"""

    def test_start_loop_disabled(self):
        p = make_plugin(config={"weather_push_enable": False})
        asyncio.run(p._start_push_loop())
        self.assertFalse(p._running)
        self.assertIsNone(p._task)

    def test_start_loop_enabled_and_terminate(self):
        p = make_plugin(config={"weather_push_enable": True})

        async def main():
            await p._start_push_loop()
            self.assertTrue(p._running)
            self.assertIsNotNone(p._task)
            await p.terminate()
            self.assertFalse(p._running)
            self.assertIsNone(p._task)

        asyncio.run(main())

    def test_start_loop_idempotent(self):
        p = make_plugin(config={"weather_push_enable": True})

        async def main():
            await p._start_push_loop()
            first = p._task
            await p._start_push_loop()  # 重复启动不新建任务
            self.assertIs(p._task, first)
            await p.terminate()

        asyncio.run(main())


class TestDirtyConfig(unittest.TestCase):
    """配置脏值防御：safe int/bool、时间格式、群列表、平台、默认城市"""

    def test_safe_int(self):
        p = make_plugin()
        self.assertEqual(p._safe_int("25"), 25)
        self.assertEqual(p._safe_int(25.7), 25)
        self.assertEqual(p._safe_int("25.0"), 25)
        self.assertEqual(p._safe_int("abc"), None)
        self.assertEqual(p._safe_int(None), None)
        self.assertEqual(p._safe_int("abc", 10), 10)

    def test_safe_bool(self):
        p = make_plugin()
        self.assertTrue(p._safe_bool(True))
        self.assertTrue(p._safe_bool("true"))
        self.assertTrue(p._safe_bool("1"))
        self.assertTrue(p._safe_bool("on"))
        self.assertTrue(p._safe_bool(1))
        self.assertFalse(p._safe_bool(False))
        self.assertFalse(p._safe_bool("off"))
        self.assertFalse(p._safe_bool("0"))
        self.assertFalse(p._safe_bool("garbage"))  # 脏值回退默认 False
        self.assertTrue(p._safe_bool("garbage", True))  # 显式默认生效

    def test_push_time_dirty_fallback(self):
        p = make_plugin()
        p.config["weather_push_time"] = "not-a-time"
        self.assertEqual(p._push_time(), "07:30")
        p.config["weather_push_time"] = "25:99"
        self.assertEqual(p._push_time(), "07:30")
        p.config["weather_push_time"] = "7:30"
        self.assertEqual(p._push_time(), "07:30")  # 非严格 HH:MM 视为脏值
        p.config["weather_push_time"] = "07:45"
        self.assertEqual(p._push_time(), "07:45")
        p.config["weather_push_time"] = None
        self.assertEqual(p._push_time(), "07:30")

    def test_push_groups_dirty(self):
        p = make_plugin()
        self.assertEqual(p._push_groups(), [])
        p.config["weather_push_groups"] = "123, 456,"
        self.assertEqual(p._push_groups(), ["123", "456"])
        p.config["weather_push_groups"] = 12345  # 非字符串/列表 → 空
        self.assertEqual(p._push_groups(), [])
        p.config["weather_push_groups"] = None
        self.assertEqual(p._push_groups(), [])
        p.config["weather_push_groups"] = ["1", 2, "3"]
        self.assertEqual(p._push_groups(), ["1", "2", "3"])

    def test_push_platform_dirty(self):
        p = make_plugin()
        self.assertEqual(p._push_platform(), "onebot")
        p.config["weather_push_platform"] = ""
        self.assertEqual(p._push_platform(), "onebot")
        p.config["weather_push_platform"] = None
        self.assertEqual(p._push_platform(), "onebot")
        p.config["weather_push_platform"] = "aiocqhttp"
        self.assertEqual(p._push_platform(), "aiocqhttp")

    def test_push_enable_dirty(self):
        p = make_plugin()
        self.assertFalse(p._push_enabled())
        p.config["weather_push_enable"] = "yes"
        self.assertTrue(p._push_enabled())
        p.config["weather_push_enable"] = 1
        self.assertTrue(p._push_enabled())
        p.config["weather_push_enable"] = "garbage"
        self.assertFalse(p._push_enabled())

    def test_default_city_dirty(self):
        p = make_plugin(config={"default_city": "  广州  "})
        self.assertEqual(p._default_city(), "广州")
        p.config["default_city"] = 123
        self.assertEqual(p._default_city(), "")
        p.config["default_city"] = None
        self.assertEqual(p._default_city(), "")

    def test_target_umo(self):
        p = make_plugin()
        self.assertEqual(p._target_umo("123"), "onebot:GroupMessage:123")
        p.config["weather_push_platform"] = "aiocqhttp"
        self.assertEqual(p._target_umo("123"), "aiocqhttp:GroupMessage:123")


if __name__ == "__main__":
    unittest.main(verbosity=1)