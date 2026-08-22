# -*- coding: utf-8 -*-
"""天气插件新功能测试（v1.1.0）：多日预报、生活指数、天气提醒、子命令分发、配置脏值防御。

所有测试均 mock 天气抓取（_fetch_raw/_fetch_weather/_fetch_forecast），不联网、不写真实 plugin_data。
"""

import asyncio
import sys
import tempfile
import unittest

sys.path.insert(0, r"D:\astrbot\data\plugins")

from astrbot_plugin_weather.main import WeatherPlugin  # noqa: E402


class FakeSession:
    def __init__(self, umo="onebot:GroupMessage:123"):
        self.umo = umo

    def __str__(self):
        return self.umo


class FakeEvent:
    def __init__(self, message_str="/天气", umo="onebot:GroupMessage:123"):
        self.session = FakeSession(umo)
        self.message_str = message_str

    def get_sender_id(self):
        return "10001"

    def chain_result(self, chain):
        return chain


class FakeContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True


def make_plugin(config=None):
    tmp = tempfile.mkdtemp(prefix="weather_extra_test_")
    cfg = dict(config or {})
    cfg["data_dir"] = tmp
    return WeatherPlugin(FakeContext(), cfg)


def run(coro):
    return asyncio.run(coro)


def chain_text(chain) -> str:
    return getattr(chain, "chain", chain)[0].text


def make_hour(chance, desc="晴"):
    return {"chanceofrain": str(chance), "weatherDesc": [{"value": desc}]}


def make_day(date, tmin, tmax, hours):
    return {"date": date, "mintempC": str(tmin), "maxtempC": str(tmax), "hourly": hours}


def make_j1():
    """模拟 wttr.in j1 响应：3 天数据 + 当前条件"""
    return {
        "current_condition": [{
            "temp_C": "28",
            "humidity": "70",
            "uvIndex": "8",
            "windspeedKmph": "12",
            "winddir16Point": "SE",
            "weatherDesc": [{"value": "多云"}],
        }],
        "weather": [
            make_day("2026-08-22", 24, 35, [make_hour(10), make_hour(20, "多云"), make_hour(90, "雷阵雨")]),
            make_day("2026-08-23", 22, 30, [make_hour(5)]),
            make_day("2026-08-24", -2, 8, []),
        ],
    }


FIXED_WEATHER = {
    "city": "北京",
    "date": "2026-08-22",
    "text": "晴",
    "temp_min": 21,
    "temp_max": 31,
    "humidity": 60,
    "wind": "SE 11km/h",
    "uv": 3,
    "rain_chance": 10,
}


class TestForecastDaysConfig(unittest.TestCase):
    def test_default_3(self):
        self.assertEqual(make_plugin()._forecast_days(), 3)

    def test_dirty_fallback(self):
        self.assertEqual(make_plugin({"weather_forecast_days": None})._forecast_days(), 3)
        self.assertEqual(make_plugin({"weather_forecast_days": "abc"})._forecast_days(), 3)

    def test_clamp_range(self):
        self.assertEqual(make_plugin({"weather_forecast_days": 0})._forecast_days(), 1)
        self.assertEqual(make_plugin({"weather_forecast_days": 99})._forecast_days(), 7)
        self.assertEqual(make_plugin({"weather_forecast_days": 5})._forecast_days(), 5)


class TestParseForecast(unittest.TestCase):
    def test_parse_three_days_with_labels(self):
        days = make_plugin()._parse_forecast_data("北京", make_j1())
        self.assertEqual(len(days), 3)
        self.assertEqual([d["label"] for d in days], ["今天", "明天", "后天"])
        self.assertEqual([d["date"] for d in days], ["2026-08-22", "2026-08-23", "2026-08-24"])

    def test_parse_mid_hour_text(self):
        # 天气现象取正午时段描述
        days = make_plugin()._parse_forecast_data("北京", make_j1())
        self.assertEqual(days[0]["text"], "多云")

    def test_parse_rain_max(self):
        days = make_plugin()._parse_forecast_data("北京", make_j1())
        self.assertEqual(days[0]["rain_chance"], 90)
        self.assertEqual(days[1]["rain_chance"], 5)

    def test_parse_temp_range(self):
        days = make_plugin()._parse_forecast_data("北京", make_j1())
        self.assertEqual((days[0]["temp_min"], days[0]["temp_max"]), (24, 35))
        self.assertEqual((days[2]["temp_min"], days[2]["temp_max"]), (-2, 8))

    def test_parse_respects_limit_config(self):
        p = make_plugin({"weather_forecast_days": 2})
        days = p._parse_forecast_data("北京", make_j1())
        self.assertEqual(len(days), 2)

    def test_parse_empty_and_garbage(self):
        self.assertEqual(make_plugin()._parse_forecast_data("北京", None), [])
        self.assertEqual(make_plugin()._parse_forecast_data("北京", {}), [])
        self.assertEqual(make_plugin()._parse_forecast_data("北京", {"weather": []}), [])

    def test_fetch_forecast_mocked_raw(self):
        p = make_plugin()
        p._fetch_raw = lambda city: make_j1() if city == "北京" else None
        self.assertEqual(len(p._fetch_forecast("北京")), 3)
        self.assertEqual(p._fetch_forecast("不存在"), [])
        self.assertEqual(p._fetch_forecast(""), [])


class TestFormatForecast(unittest.TestCase):
    def _days(self):
        return make_plugin()._parse_forecast_data("北京", make_j1())

    def test_header_and_labels(self):
        text = make_plugin()._format_forecast("北京", self._days())
        self.assertIn("北京", text)
        self.assertIn("未来 3 天预报", text)
        self.assertIn("今天:", text)
        self.assertIn("明天:", text)
        self.assertIn("后天:", text)

    def test_temp_and_rain_lines(self):
        text = make_plugin()._format_forecast("北京", self._days())
        self.assertIn("24°C ~ 35°C", text)
        self.assertIn("降雨概率 90%", text)

    def test_high_temp_alert_attached(self):
        # 今天 max 35°C 触发高温提醒
        text = make_plugin()._format_forecast("北京", self._days())
        self.assertIn("⚠️ 天气提醒", text)
        self.assertIn("高温提醒", text)

    def test_alert_disabled_no_section(self):
        p = make_plugin({"weather_alert_enabled": False})
        text = p._format_forecast("北京", self._days())
        self.assertNotIn("⚠️ 天气提醒", text)

    def test_empty_days(self):
        text = make_plugin()._format_forecast("北京", [])
        self.assertIn("未来 0 天预报", text)


class TestLifeIndices(unittest.TestCase):
    def test_four_indices_present(self):
        names = [n for n, _ in make_plugin()._life_indices(dict(FIXED_WEATHER))]
        self.assertEqual(names, ["穿衣", "洗车", "紫外线", "运动"])

    def test_carwash_suitable_clear(self):
        idx = dict(make_plugin()._life_indices(dict(FIXED_WEATHER)))
        self.assertIn("适宜洗车", idx["洗车"])

    def test_carwash_not_suitable_rainy_text(self):
        w = dict(FIXED_WEATHER, text="中雨")
        idx = dict(make_plugin()._life_indices(w))
        self.assertIn("不宜洗车", idx["洗车"])

    def test_carwash_not_suitable_high_chance(self):
        w = dict(FIXED_WEATHER, rain_chance=80)
        idx = dict(make_plugin()._life_indices(w))
        self.assertIn("不宜洗车", idx["洗车"])

    def test_uv_levels(self):
        cases = {1: "弱", 4: "中等", 6: "强", 9: "很强", 11: "极强"}
        for uv, level in cases.items():
            w = dict(FIXED_WEATHER, uv=uv)
            idx = dict(make_plugin()._life_indices(w))
            self.assertIn(level, idx["紫外线"], f"uv={uv}")

    def test_uv_missing(self):
        w = dict(FIXED_WEATHER)
        w.pop("uv")
        idx = dict(make_plugin()._life_indices(w))
        self.assertIn("暂无数据", idx["紫外线"])

    def test_sport_indoor_when_rainy(self):
        w = dict(FIXED_WEATHER, text="大雨")
        idx = dict(make_plugin()._life_indices(w))
        self.assertIn("室内运动", idx["运动"])

    def test_sport_outdoor_when_comfortable(self):
        idx = dict(make_plugin()._life_indices(dict(FIXED_WEATHER)))
        self.assertIn("适宜户外运动", idx["运动"])

    def test_sport_extreme_heat(self):
        w = dict(FIXED_WEATHER, temp_min=33, temp_max=39)
        idx = dict(make_plugin()._life_indices(w))
        self.assertIn("不建议户外运动", idx["运动"])

    def test_format_indices_contains_all(self):
        text = make_plugin()._format_indices(dict(FIXED_WEATHER))
        for kw in ("生活指数", "穿衣", "洗车", "紫外线", "运动", "2026-08-22"):
            self.assertIn(kw, text)


class TestWeatherAlerts(unittest.TestCase):
    def test_disabled_returns_empty(self):
        p = make_plugin({"weather_alert_enabled": False})
        self.assertEqual(p._weather_alerts(dict(FIXED_WEATHER, temp_max=40)), [])

    def test_empty_day(self):
        self.assertEqual(make_plugin()._weather_alerts(None), [])
        self.assertEqual(make_plugin()._weather_alerts({}), [])

    def test_high_temp(self):
        alerts = make_plugin()._weather_alerts(dict(FIXED_WEATHER, temp_max=38))
        self.assertTrue(any("高温" in a for a in alerts))

    def test_low_temp(self):
        alerts = make_plugin()._weather_alerts(dict(FIXED_WEATHER, temp_min=-5))
        self.assertTrue(any("低温" in a for a in alerts))

    def test_strong_wind(self):
        alerts = make_plugin()._weather_alerts(dict(FIXED_WEATHER, wind="N 55km/h"))
        self.assertTrue(any("大风" in a and "55" in a for a in alerts))

    def test_thunderstorm(self):
        alerts = make_plugin()._weather_alerts(dict(FIXED_WEATHER, text="雷阵雨"))
        self.assertTrue(any("强对流" in a for a in alerts))

    def test_fog(self):
        alerts = make_plugin()._weather_alerts(dict(FIXED_WEATHER, text="雾"))
        self.assertTrue(any("大雾" in a for a in alerts))

    def test_rain_chance_high_without_rain_text(self):
        alerts = make_plugin()._weather_alerts(dict(FIXED_WEATHER, rain_chance=85))
        self.assertTrue(any("降雨" in a and "85" in a for a in alerts))

    def test_normal_weather_no_alerts(self):
        self.assertEqual(make_plugin()._weather_alerts(dict(FIXED_WEATHER)), [])


class TestCommandDispatch(unittest.TestCase):
    def test_forecast_command(self):
        p = make_plugin()
        p._fetch_forecast = lambda city: p._parse_forecast_data(city, make_j1()) if city == "北京" else []
        r = run(p.weather_cmd(FakeEvent("/天气 预报 北京")))
        text = chain_text(r)
        self.assertIn("未来 3 天预报", text)
        self.assertIn("今天:", text)

    def test_future_alias(self):
        p = make_plugin()
        p._fetch_forecast = lambda city: p._parse_forecast_data(city, make_j1())
        r = run(p.weather_cmd(FakeEvent("/天气 未来 上海")))
        self.assertIn("上海", chain_text(r))

    def test_indices_command(self):
        p = make_plugin()
        p._fetch_weather = lambda city: dict(FIXED_WEATHER)
        r = run(p.weather_cmd(FakeEvent("/天气 指数 北京")))
        text = chain_text(r)
        self.assertIn("生活指数", text)
        self.assertIn("适宜户外运动", text)

    def test_current_still_works(self):
        p = make_plugin()
        p._fetch_weather = lambda city: dict(FIXED_WEATHER)
        r = run(p.weather_cmd(FakeEvent("/天气 北京")))
        text = chain_text(r)
        self.assertIn("北京 天气预报", text)
        self.assertIn("21°C ~ 31°C", text)

    def test_default_city_used_for_subcommand(self):
        p = make_plugin({"default_city": "成都"})
        seen = {}
        p._fetch_forecast = lambda city: seen.setdefault("city", city) or []
        run(p.weather_cmd(FakeEvent("/天气 预报")))
        self.assertEqual(seen.get("city"), "成都")

    def test_forecast_failure_message(self):
        p = make_plugin()
        p._fetch_forecast = lambda city: []
        r = run(p.weather_cmd(FakeEvent("/天气 预报 北京")))
        self.assertIn("失败", chain_text(r))

    def test_indices_failure_message(self):
        p = make_plugin()
        p._fetch_weather = lambda city: None
        r = run(p.weather_cmd(FakeEvent("/天气 指数 北京")))
        self.assertIn("失败", chain_text(r))

    def test_remember_city_after_forecast(self):
        p = make_plugin()
        p._fetch_forecast = lambda city: p._parse_forecast_data(city, make_j1())
        ev = FakeEvent("/天气 预报 杭州")
        run(p.weather_cmd(ev))
        self.assertEqual(p._recent_cities.get(str(ev.session)), "杭州")


if __name__ == "__main__":
    unittest.main(verbosity=1)
