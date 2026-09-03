from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timezone
from pathlib import Path

import morning_brief as mb


def daily_bar(session_date: date, close: float) -> dict:
    timestamp = datetime.combine(session_date, time.min, tzinfo=mb.NY_TZ)
    return {"t": timestamp.astimezone(timezone.utc).isoformat(), "c": close}


def minute_bar(
    session_date: date,
    hour: int,
    minute: int,
    close: float,
    volume: int,
    *,
    high: float | None = None,
    low: float | None = None,
) -> dict:
    timestamp = datetime.combine(session_date, time(hour, minute), tzinfo=mb.NY_TZ)
    return {
        "t": timestamp.astimezone(timezone.utc).isoformat(),
        "c": close,
        "h": high if high is not None else close,
        "l": low if low is not None else close,
        "v": volume,
    }


def article(
    headline: str,
    source: str,
    session_date: date,
    *,
    summary: str = "",
    symbols: list[str] | None = None,
    authority: int = 4,
    url: str = "https://example.com/story",
    section: str = "market",
) -> dict:
    created = datetime.combine(session_date, time(20, 0), tzinfo=timezone.utc)
    return {
        "headline": headline,
        "summary": summary,
        "content": summary,
        "url": url,
        "source": source,
        "symbols": symbols or [],
        "created_at": created.isoformat(),
        "_section": section,
        "_authority": authority,
    }


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeNewsClient:
    def __init__(self):
        self.calls: list[dict] = []

    def get(self, _url, *, headers, params, timeout):
        self.calls.append(dict(params))
        if len(self.calls) == 1:
            return FakeResponse(
                {
                    "news": [
                        {
                            "headline": "First page",
                            "source": "benzinga",
                            "symbols": ["SPY"],
                        }
                    ],
                    "next_page_token": "page-2",
                }
            )
        return FakeResponse(
            {
                "news": [
                    {
                        "headline": "Second page",
                        "source": "benzinga",
                        "symbols": ["QQQ"],
                    }
                ],
                "next_page_token": None,
            }
        )


class FakeRSSClient:
    class Response:
        content = b"""<?xml version='1.0'?>
        <rss><channel><item>
          <title>Wall Street rises after inflation cools</title>
          <link>https://example.com/rss-story</link>
          <description>Stocks gained after a cooler inflation report.</description>
          <pubDate>Fri, 24 Jul 2026 20:15:00 GMT</pubDate>
        </item></channel></rss>"""

        def raise_for_status(self) -> None:
            return None

    def get(self, _url, *, headers, timeout):
        return self.Response()


class FakeBarClient:
    def __init__(self, bars: dict[str, list[dict]]):
        self.bars = bars
        self.calls: list[dict] = []

    def get(self, _url, *, headers, params, timeout):
        self.calls.append(dict(params))
        return FakeResponse({"bars": self.bars, "next_page_token": None})


class MarketPulseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.thursday = date(2026, 7, 23)
        self.friday = date(2026, 7, 24)
        self.monday = date(2026, 7, 27)
        self.raw = {
            "SPY": [
                daily_bar(self.thursday, 610),
                daily_bar(self.friday, 604),
                daily_bar(self.monday, 590),
            ],
            "QQQ": [
                daily_bar(self.thursday, 525),
                daily_bar(self.friday, 514),
                daily_bar(self.monday, 500),
            ],
            "DIA": [
                daily_bar(self.thursday, 447),
                daily_bar(self.friday, 445),
                daily_bar(self.monday, 442),
            ],
            "IWM": [
                daily_bar(self.thursday, 224),
                daily_bar(self.friday, 220),
                daily_bar(self.monday, 215),
            ],
            "SMH": [
                daily_bar(self.thursday, 296),
                daily_bar(self.friday, 280),
                daily_bar(self.monday, 270),
            ],
        }

    def test_morning_excludes_incomplete_current_session(self) -> None:
        now = datetime(2026, 7, 27, 13, 0, tzinfo=timezone.utc)  # 9 AM EDT
        pulse = mb.build_market_pulse(self.raw, now, "sip")
        self.assertEqual(pulse.session_date, self.friday)
        self.assertAlmostEqual(pulse.moves["SPY"].change_pct, -0.9836, places=3)

    def test_after_close_includes_current_session(self) -> None:
        now = datetime(2026, 7, 27, 21, 0, tzinfo=timezone.utc)  # 5 PM EDT
        pulse = mb.build_market_pulse(self.raw, now, "sip")
        self.assertEqual(pulse.session_date, self.monday)
        self.assertEqual(pulse.status, "strong_down")

    def test_mismatched_session_is_not_spliced(self) -> None:
        now = datetime(2026, 7, 27, 21, 0, tzinfo=timezone.utc)
        raw = {"SPY": [daily_bar(self.friday, 604)]}
        pulse = mb.build_market_pulse(raw, now, "sip")
        self.assertFalse(pulse.available)
        self.assertIn("两个可比较", pulse.note)


class PremarketPulseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prior_session = date(2026, 7, 27)
        self.premarket_session = date(2026, 7, 28)
        self.now = datetime(2026, 7, 28, 13, 0, tzinfo=timezone.utc)  # 9 AM EDT
        moves = {
            "SPY": mb.MarketMove("SPY", "S&P 500", 600, 598, 0.33, self.prior_session, "broad"),
            "QQQ": mb.MarketMove("QQQ", "Nasdaq 100", 500, 497, 0.60, self.prior_session, "broad"),
            "IWM": mb.MarketMove("IWM", "Russell 2000", 220, 219, 0.46, self.prior_session, "broad"),
            "SMH": mb.MarketMove("SMH", "Semiconductors", 280, 275, 1.82, self.prior_session, "sector"),
            "MRVL": mb.MarketMove("MRVL", "Marvell", 88, 87, 1.15, self.prior_session, "special"),
            "SPCX": mb.MarketMove("SPCX", "SPCX", 112, 111, 0.90, self.prior_session, "special"),
        }
        self.market_pulse = mb.MarketPulse(
            self.prior_session, moves, "up", "sip", "test daily bars"
        )

    def test_builds_delayed_premarket_move_without_splicing_regular_session(self) -> None:
        raw = {
            "SPY": [
                minute_bar(self.prior_session, 18, 0, 601, 900),
                minute_bar(self.premarket_session, 4, 5, 602, 1_000, high=602.5, low=601.5),
                minute_bar(self.premarket_session, 8, 40, 606, 2_500, high=607, low=605),
                minute_bar(self.premarket_session, 9, 30, 610, 50_000),
            ],
            "QQQ": [minute_bar(self.premarket_session, 8, 39, 505, 3_000)],
            "IWM": [minute_bar(self.premarket_session, 8, 38, 219, 1_500)],
        }
        premarket = mb.build_premarket_pulse(raw, self.market_pulse, self.now, "sip")

        spy = premarket.moves["SPY"]
        self.assertTrue(premarket.available)
        self.assertEqual(spy.price, 606)
        self.assertEqual(spy.previous_close, 600)
        self.assertAlmostEqual(spy.change_pct, 1.0)
        self.assertEqual(spy.volume, 3_500)
        self.assertEqual(spy.high, 607)
        self.assertEqual(spy.low, 601.5)
        self.assertEqual(spy.as_of.astimezone(mb.NY_TZ).time(), time(8, 40))
        self.assertNotEqual(spy.price, 610)
        self.assertEqual(premarket.status, "盘前明显分化")

    def test_fetch_uses_one_minute_bars_and_delayed_query_end(self) -> None:
        client = FakeBarClient(
            {"SPY": [minute_bar(self.premarket_session, 8, 40, 606, 2_500)]}
        )
        premarket = mb.fetch_premarket_pulse(
            "key", "secret", self.market_pulse, self.now, client=client
        )

        self.assertTrue(premarket.available)
        self.assertEqual(client.calls[0]["timeframe"], "1Min")
        self.assertEqual(client.calls[0]["symbols"], ",".join(mb.PREMARKET_SYMBOLS))
        self.assertEqual(client.calls[0]["feed"], "sip")
        self.assertEqual(client.calls[0]["start"], "2026-07-28T08:00:00Z")
        self.assertEqual(client.calls[0]["end"], "2026-07-28T12:44:00Z")

    def test_regular_session_and_weekends_are_not_labeled_premarket(self) -> None:
        regular = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)
        weekend = datetime(2026, 7, 26, 13, 0, tzinfo=timezone.utc)
        self.assertFalse(
            mb.build_premarket_pulse({}, self.market_pulse, regular, "sip").active
        )
        self.assertFalse(
            mb.build_premarket_pulse({}, self.market_pulse, weekend, "sip").active
        )


class RankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = date(2026, 7, 24)
        moves = {
            "SPY": mb.MarketMove("SPY", "S&P 500", 600, 610, -1.64, self.session, "broad"),
            "QQQ": mb.MarketMove("QQQ", "Nasdaq 100", 500, 520, -3.85, self.session, "broad"),
            "DIA": mb.MarketMove("DIA", "Dow", 440, 444, -0.90, self.session, "broad"),
        }
        self.pulse = mb.MarketPulse(self.session, moves, "strong_down", "sip", "test")

    def test_market_recap_beats_listicle_and_generic_war_story(self) -> None:
        recap = article(
            "Wall Street falls as hot jobs data lifts rate expectations and chips slide",
            "CNBC Markets",
            self.session,
            summary="Stocks closed sharply lower after a strong jobs report pushed Treasury yields higher.",
            symbols=["SPY", "QQQ", "NVDA"],
            authority=6,
            url="https://cnbc.example/recap",
        )
        listicle = article(
            "3 Stocks At The Center Of The New AI Order",
            "Benzinga",
            self.session,
            summary="Three technology stocks investors may want to watch.",
            symbols=["GOOGL", "NVDA"],
            authority=2,
            url="https://benzinga.example/Opinion/listicle",
        )
        war_story = article(
            "Kyiv neighbourhood hit in Russian strike",
            "BBC Business",
            self.session,
            summary="Residents sheltered after a military attack.",
            authority=4,
            url="https://bbc.example/world",
        )
        history = article(
            "S&P 500 chases a winning streak last seen in 1995",
            "Benzinga",
            self.session,
            symbols=["SPY"],
            authority=2,
            url="https://benzinga.example/history",
        )
        feature = article(
            "He brought AI to Wall Street in 1994 and now teaches investing",
            "MarketWatch",
            self.session,
            summary="A profile of an early artificial intelligence investor.",
            authority=5,
            url="https://marketwatch.example/profile",
        )

        selected = mb.select_market_drivers(
            [listicle, history, feature, war_story, recap], self.pulse
        )
        self.assertEqual(selected[0]["headline"], recap["headline"])
        selected_headlines = {item["headline"] for item in selected}
        self.assertNotIn(listicle["headline"], selected_headlines)
        self.assertNotIn(war_story["headline"], selected_headlines)
        self.assertNotIn(history["headline"], selected_headlines)
        self.assertNotIn(feature["headline"], selected_headlines)

    def test_no_evidence_means_no_forced_cause(self) -> None:
        assessment = mb.build_driver_assessment(self.pulse, [])
        self.assertEqual(assessment.confidence, "不足")
        self.assertIn("暂不强行归因", assessment.summary_zh)

    def test_different_day_recap_is_never_joined_to_session(self) -> None:
        stale = article(
            "Wall Street falls as rate fears hit technology stocks",
            "CNBC Markets",
            self.session.replace(day=23),
            summary="Stocks closed lower as Treasury yields rose.",
            symbols=["SPY", "QQQ"],
            authority=6,
        )
        self.assertEqual(mb.select_market_drivers([stale], self.pulse), [])

    def test_opposite_intraday_direction_is_not_used_for_the_close(self) -> None:
        earlier_rally = article(
            "U.S. stocks rise as investors cheer cooler inflation",
            "MarketWatch",
            self.session,
            summary="Wall Street gained in morning trading after an inflation update.",
            symbols=["SPY", "QQQ"],
            authority=5,
        )
        erase_gains = article(
            "U.S. stocks erase gains as chipmakers extend losses",
            "Bloomberg Markets",
            self.session,
            summary="The S&P 500 closed lower as semiconductor shares extended losses.",
            symbols=["SPY", "QQQ", "NVDA"],
            authority=7,
            url="https://bloomberg.example/close",
        )

        self.assertEqual(mb._article_direction(earlier_rally), 1)
        self.assertEqual(mb._article_direction(erase_gains), -1)
        selected = mb.select_market_drivers([earlier_rally, erase_gains], self.pulse)
        self.assertEqual([item["headline"] for item in selected], [erase_gains["headline"]])

    def test_mixed_close_keeps_wrap_but_rejects_premarket_and_generic_ai(self) -> None:
        mixed_pulse = mb.MarketPulse(
            self.session,
            {
                "SPY": mb.MarketMove("SPY", "S&P 500", 610, 610, 0.02, self.session, "broad"),
                "QQQ": mb.MarketMove("QQQ", "Nasdaq 100", 518, 520, -0.31, self.session, "broad"),
                "DIA": mb.MarketMove("DIA", "Dow", 446, 444, 0.48, self.session, "broad"),
            },
            "mixed",
            "sip",
            "test",
        )
        premarket = article(
            "Stock Market Today: S&P 500 Futures Rise as chip shares rebound",
            "Benzinga",
            self.session,
            summary="U.S. stocks were set to open higher before the bell.",
            symbols=["SPY", "QQQ", "NVDA"],
            authority=5,
            url="https://benzinga.example/futures",
        )
        generic_ai = article(
            "Sundar Pichai Backs Jensen Huang's Open-Weight AI Push as OpenAI Joins Letter",
            "Benzinga",
            self.session,
            summary=(
                "Wall Street followed the technology industry letter. "
                "A related market note said the S&P 500 finished little changed."
            ),
            symbols=["GOOG", "NVDA", "AMZN"],
            authority=5,
            url="https://benzinga.example/ai-letter",
        )
        single_stock = article(
            "SpaceX Stock Slides but Cathie Wood Keeps Buying",
            "Benzinga",
            self.session,
            summary="The broader stock market was mixed while investors watched the company.",
            symbols=["SPCX", "TSLA"],
            authority=5,
            url="https://benzinga.example/single-stock",
        )
        previous_session_wrap = article(
            "Dow Gains Over 200 Points But Records Weekly Loss",
            "Benzinga",
            self.session,
            summary="The Dow and S&P 500 closed the prior week lower.",
            symbols=["DIA", "SPY"],
            authority=5,
            url="https://benzinga.example/weekly-wrap",
        )
        previous_session_wrap["created_at"] = datetime.combine(
            self.session, time(3, 26), tzinfo=mb.NY_TZ
        ).astimezone(timezone.utc).isoformat()
        closing_wrap = article(
            "Chipmakers Sink as Wall Street Rotation Powers On: Markets Wrap",
            "Bloomberg Markets",
            self.session,
            summary="The S&P 500 finished little changed as chipmakers fell and financial shares advanced.",
            authority=7,
            url="https://bloomberg.example/market-wrap",
        )

        selected = mb.select_market_drivers(
            [
                premarket,
                generic_ai,
                single_stock,
                previous_session_wrap,
                closing_wrap,
            ],
            mixed_pulse,
        )
        self.assertEqual(
            [item["headline"] for item in selected],
            [closing_wrap["headline"]],
        )


class FetchTests(unittest.TestCase):
    def test_alpaca_news_follows_page_token(self) -> None:
        client = FakeNewsClient()
        start = datetime(2026, 7, 24, tzinfo=timezone.utc)
        end = datetime(2026, 7, 25, tzinfo=timezone.utc)
        items = mb.fetch_alpaca_news("key", "secret", start, end, client=client)
        self.assertEqual(len(items), 2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1]["page_token"], "page-2")
        self.assertEqual(client.calls[0]["include_content"], "true")

    def test_rss_xml_is_parsed_with_timestamp_and_section(self) -> None:
        start = datetime(2026, 7, 24, tzinfo=timezone.utc)
        end = datetime(2026, 7, 25, tzinfo=timezone.utc)
        spec = mb.FeedSpec("Test Markets", "https://example.com/feed", "market", 6)
        items = mb.fetch_rss(spec, start, end, client=FakeRSSClient())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["headline"], "Wall Street rises after inflation cools")
        self.assertEqual(items[0]["_section"], "market")
        self.assertIsNotNone(mb.parse_ts(items[0]["created_at"]))


class WeeklyEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)  # Sunday 1 PM ET

    def test_weekend_previews_the_coming_market_week(self) -> None:
        self.assertEqual(
            mb.market_week_dates(self.now),
            (date(2026, 8, 31), date(2026, 9, 6)),
        )
        weekday = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(
            mb.market_week_dates(weekday),
            (date(2026, 8, 31), date(2026, 9, 6)),
        )

    def test_verified_calendar_loads_events_and_monitored_positions(self) -> None:
        calendar = mb.load_weekly_event_calendar(self.now)
        self.assertEqual(len(calendar.events), 6)
        self.assertEqual(calendar.events[0].title_zh, "美国 7 月 JOLTS 职位空缺")
        self.assertEqual(calendar.events[-1].title_zh, "美国 8 月就业报告")
        self.assertEqual(
            calendar.portfolio_symbols,
            ("SMH", "VOO", "QQQM", "MRVL", "SPCX"),
        )
        self.assertTrue(all(event.source_url.startswith("https://") for event in calendar.events))
        self.assertTrue(all(event.event_id for event in calendar.events))
        self.assertTrue(all(event.result is None for event in calendar.events))

    def test_published_results_load_without_leaking_future_results(self) -> None:
        released_now = datetime(2026, 9, 3, 22, 10, tzinfo=timezone.utc)
        calendar = mb.load_weekly_event_calendar(released_now)

        self.assertTrue(all(event.result is not None for event in calendar.events[:5]))
        self.assertIsNone(calendar.events[-1].result)
        services = calendar.events[4].result
        self.assertIsNotNone(services)
        self.assertEqual(services.metrics[0].actual, "55.4")
        self.assertEqual(services.metrics[0].expected, "54.3")
        self.assertIn("QQQM", services.portfolio_impact_zh)

    def test_current_news_pull_can_fill_ism_result(self) -> None:
        now = datetime(2026, 9, 3, 20, 30, tzinfo=timezone.utc)
        calendar = mb.load_weekly_event_calendar(now)
        services = replace(calendar.events[4], result=None)
        calendar = replace(calendar, events=[*calendar.events[:4], services, calendar.events[5]])
        release = {
            "headline": "Gold Jumps 3%; ISM Services PMI Rises In August",
            "summary": (
                "ISM services PMI surged to 55.4 in August from 54.1, "
                "topping market estimates of 54.3."
            ),
            "content": "",
            "url": "https://example.com/ism-services",
            "source": "Benzinga",
            "created_at": "2026-09-03T14:05:00+00:00",
        }

        refreshed = mb.update_weekly_event_results(calendar, [release], now)
        result = refreshed.events[4].result

        self.assertIsNotNone(result)
        self.assertEqual(result.verdict_zh, "增长强于预期")
        self.assertEqual(result.metrics[0].actual, "55.4")
        self.assertEqual(result.metrics[0].expected, "54.3")
        self.assertEqual(result.metrics[0].previous, "54.1")

    def test_current_news_pull_parses_negative_payroll_result(self) -> None:
        now = datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc)
        event = mb.load_weekly_event_calendar(now).events[-1]
        release = {
            "headline": "US employers cut jobs in August",
            "summary": (
                "Nonfarm payrolls contracted by 23,000 versus a consensus estimate "
                "of 83,000. The unemployment rate came in at 4.1% versus 4.2%. "
                "Average hourly earnings rose 0.1% month-over-month."
            ),
            "content": "",
            "url": "https://example.com/jobs-report",
            "source": "Benzinga",
            "created_at": "2026-09-04T12:31:00+00:00",
        }

        result = mb._employment_result_from_article(event, release)

        self.assertIsNotNone(result)
        self.assertEqual(result.verdict_zh, "就业明显转弱")
        self.assertEqual(result.metrics[0].actual, "-23K")
        self.assertEqual(result.metrics[0].expected, "83K")
        self.assertEqual(result.metrics[1].actual, "4.1%")
        self.assertEqual(result.metrics[1].expected, "4.2%")

    def test_employment_parser_handles_release_shorthand(self) -> None:
        now = datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc)
        event = mb.load_weekly_event_calendar(now).events[-1]
        release = {
            "headline": "Nasdaq gains as US jobs report lands",
            "summary": (
                "Nonfarm payrolls came at -23K vs 86K consensus. "
                "Unemployment 4.1% vs 4.2% expected. "
                "Average hourly earnings 0.1% vs 0.3%."
            ),
            "url": "https://example.com/jobs-shorthand",
            "source": "Benzinga",
            "created_at": "2026-09-04T12:31:00+00:00",
        }

        result = mb._employment_result_from_article(event, release)

        self.assertIsNotNone(result)
        self.assertEqual(result.metrics[0].actual, "-23K")
        self.assertEqual(result.metrics[0].expected, "86K")
        self.assertEqual(result.metrics[1].expected, "4.2%")
        self.assertEqual(result.metrics[2].actual, "0.1%")

    def test_event_result_cache_survives_next_render(self) -> None:
        now = datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc)
        calendar = mb.load_weekly_event_calendar(now)
        employment = calendar.events[-1]
        result = mb.EventResult(
            published_at=datetime(2026, 9, 4, 12, 31, tzinfo=timezone.utc),
            source="Test source",
            source_url="https://example.com/result",
            verdict_zh="已公布",
            summary_zh="测试结果",
            sector_impact_zh="测试板块影响",
            portfolio_impact_zh="测试仓位影响",
            metrics=(mb.EventMetric("非农就业", "100K", "90K"),),
        )
        with_result = replace(
            calendar,
            events=[*calendar.events[:-1], replace(employment, result=result)],
        )

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / mb.EVENT_RESULTS_CACHE_NAME
            mb._write_event_results_cache(with_result, cache_path, now)
            restored = mb.merge_cached_event_results(calendar, cache_path, now)

        self.assertEqual(restored.events[-1].result.summary_zh, "测试结果")

    def test_invalid_calendar_entry_is_skipped_without_inventing_an_event(self) -> None:
        payload = {
            "verified_at": "2026-08-30T12:00:00-04:00",
            "events": [
                {
                    "starts_at": "2026-09-01T10:00:00-04:00",
                    "title_zh": "Unverified",
                    "source_url": "javascript:alert(1)",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            calendar = mb.load_weekly_event_calendar(self.now, path)
        self.assertEqual(calendar.events, [])

    def test_weekend_current_sections_exclude_friday_but_keep_weekend_news(self) -> None:
        friday = article("Friday recap", "Test", date(2026, 8, 28))
        saturday = article("Saturday update", "Test", date(2026, 8, 29))
        sunday = article("Sunday update", "Test", date(2026, 8, 30))
        selected = mb.current_section_news([friday, saturday, sunday], self.now)
        self.assertEqual(
            [item["headline"] for item in selected],
            ["Saturday update", "Sunday update"],
        )

    def test_render_and_notification_show_scenarios_sectors_and_position_impact(self) -> None:
        pulse, assessment, world = mb._demo_payload(self.now)
        calendar = mb.load_weekly_event_calendar(self.now)
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            output = mb.render_html(
                pulse,
                assessment,
                world,
                output_dir,
                self.now,
                weekly_calendar=calendar,
            )
            page = output.read_text(encoding="utf-8")
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))

        self.assertIn("本周关键事件", page)
        self.assertIn("Broadcom（AVGO）FY2026 Q3 财报与指引", page)
        self.assertIn("偏利好情景", page)
        self.assertIn("偏利空情景", page)
        self.assertIn("对当前监控仓位的影响", page)
        self.assertIn("SMH / MRVL", page)
        self.assertEqual(page.count('<details class="event-card'), 6)
        self.assertNotIn('<details class="event-card event-next" open', page)
        self.assertIn("公布后直接显示实际结果", page)
        self.assertEqual(len(status["weekly_events"]["events"]), 6)

        messages = mb._notification_messages(
            pulse,
            assessment,
            world,
            self.now,
            weekly_calendar=calendar,
        )
        self.assertIn("【本周关键事件】", messages[0][1])
        self.assertIn("美国 7 月 JOLTS 职位空缺", messages[0][1])
        self.assertIn("仓位敏感度", messages[0][1])


class BriefStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = date(2026, 7, 27)
        self.now = datetime(2026, 7, 27, 23, 0, tzinfo=timezone.utc)
        self.pulse = mb.MarketPulse(
            self.session,
            {
                "SPY": mb.MarketMove("SPY", "S&P 500", 739, 738.9, 0.02, self.session, "broad"),
                "QQQ": mb.MarketMove("QQQ", "Nasdaq 100", 682, 684, -0.31, self.session, "broad"),
                "DIA": mb.MarketMove("DIA", "Dow", 521, 518.5, 0.48, self.session, "broad"),
                "IWM": mb.MarketMove("IWM", "Russell 2000", 293, 291.3, 0.60, self.session, "broad"),
                "SMH": mb.MarketMove("SMH", "Semiconductors", 310, 317, -2.25, self.session, "sector"),
                "XLF": mb.MarketMove("XLF", "Financials", 55, 54.45, 1.01, self.session, "sector"),
                "SPCX": mb.MarketMove("SPCX", "SPCX", 112, 111, 0.90, self.session, "special"),
                "MRVL": mb.MarketMove("MRVL", "Marvell", 88, 90, -2.22, self.session, "special"),
            },
            "mixed",
            "sip",
            "test",
        )

    def test_overview_calls_three_up_one_down_cautiously_positive(self) -> None:
        assessment = mb.DriverAssessment([], "中等", "test", ["ai_chips"])
        overview = mb.build_market_overview(self.pulse, assessment)
        self.assertEqual(overview.label_zh, "中性偏利好 · 结构分化")
        self.assertEqual(overview.tone, "cautious-positive")
        self.assertIn("3 个上涨、1 个下跌", overview.evidence_zh[0])
        self.assertTrue(any("半导体" in item for item in overview.evidence_zh))

    def test_sector_news_and_spcx_mrvl_are_separate(self) -> None:
        chip_wrap = article(
            "Chipmakers Sink as Wall Street Rotation Powers On: Markets Wrap",
            "Bloomberg Markets",
            self.session,
            summary="The S&P 500 was mixed as semiconductor shares fell.",
            symbols=["SPY", "QQQ", "NVDA"],
            authority=7,
            url="https://bloomberg.example/chips-wrap",
        )
        chip_wrap["_impact_score"] = 28.0
        chip_wrap["_is_recap"] = True
        oil = article(
            "Oil Crashes as Energy Shares Retreat",
            "Financial Times Markets",
            self.session,
            summary="Crude prices fell and energy stocks declined.",
            symbols=["XLE", "XOM"],
            authority=7,
            url="https://ft.example/oil",
        )
        spcx = article(
            "SpaceX Starship completes a major flight test",
            "CNBC Markets",
            self.session,
            symbols=["SPCX"],
            authority=6,
            url="https://cnbc.example/spacex",
        )
        mrvl = article(
            "Marvell unveils a new data-center chip",
            "CNBC Markets",
            self.session,
            symbols=["MRVL"],
            authority=6,
            url="https://cnbc.example/mrvl",
        )
        stories = [chip_wrap, oil, spcx, mrvl]
        special = {
            symbol: mb.select_special_news(stories, spec, self.now)
            for symbol, spec in mb.SPECIAL_WATCH_SPECS.items()
        }
        excluded = {
            item["url"] for items in special.values() for item in items
        }
        groups = mb.build_sector_groups(
            stories,
            [chip_wrap],
            self.now,
            exclude_urls=excluded,
        )

        self.assertEqual([item["headline"] for item in special["SPCX"]], [spcx["headline"]])
        self.assertEqual([item["headline"] for item in special["MRVL"]], [mrvl["headline"]])
        grouped_headlines = {
            item["headline"] for group in groups for item in group.articles
        }
        self.assertIn(chip_wrap["headline"], grouped_headlines)
        self.assertIn(oil["headline"], grouped_headlines)
        self.assertNotIn(spcx["headline"], grouped_headlines)
        self.assertNotIn(mrvl["headline"], grouped_headlines)

    def test_notification_contains_overview_sectors_and_focus_symbols(self) -> None:
        core = article(
            "Chipmakers Sink as Wall Street Rotation Powers On: Markets Wrap",
            "Bloomberg Markets",
            self.session,
            symbols=["NVDA", "QQQ"],
            authority=7,
            url="https://bloomberg.example/core",
        )
        core["_zh_headline"] = "芯片股下跌，华尔街板块轮动延续"
        assessment = mb.DriverAssessment([core], "中等", "板块轮动", ["ai_chips"])
        overview = mb.build_market_overview(self.pulse, assessment)
        groups = [mb.SectorGroup("semiconductors", "半导体", "SMH", [core])]
        special = {"SPCX": [], "MRVL": []}

        messages = mb._notification_messages(
            self.pulse,
            assessment,
            [],
            self.now,
            overview=overview,
            sector_groups=groups,
            special_news=special,
        )
        title, body, _ = messages[0]
        self.assertIn("中性偏利好", title)
        self.assertIn("【今日总览】", body)
        self.assertIn("【板块核心新闻】", body)
        self.assertIn("半导体 昨收 SMH -2.25%", body)
        self.assertIn("SPCX / SpaceX 相关", body)
        self.assertIn("MRVL · Marvell", body)

    def test_premarket_notification_uses_extended_hours_prices(self) -> None:
        now = datetime(2026, 7, 28, 13, 10, tzinfo=timezone.utc)
        as_of = datetime(2026, 7, 28, 12, 54, tzinfo=timezone.utc)
        move = mb.PremarketMove(
            "SPY", "S&P 500", 742.5, 739, 0.47, 125_000, 743, 738.5, as_of
        )
        premarket = mb.PremarketPulse(
            date(2026, 7, 28),
            {"SPY": move},
            "盘前偏强",
            "sip",
            as_of,
            "延迟 SIP 全市场 1 分钟成交聚合（至少延迟 16 分钟）",
        )
        assessment = mb.DriverAssessment([], "不足", "test", [])
        messages = mb._notification_messages(
            self.pulse,
            assessment,
            [],
            now,
            premarket=premarket,
        )
        title, body, _ = messages[0]
        self.assertIn("Premarket Brief", title)
        self.assertIn("【盘前行情", body)
        self.assertIn("SPY $742.50 +0.47%", body)
        self.assertIn("涨跌相对昨收", body)


class ScheduleTests(unittest.TestCase):
    def test_workflow_uses_continuous_refreshes_and_separate_notifications(self) -> None:
        workflow = (mb.ROOT / ".github" / "workflows" / "morning-brief.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "7-59/15 * * * *"', workflow)
        self.assertIn('"10 13 * * 1-5"|"10 14 * * 1-5"', workflow)
        self.assertIn("python morning_brief.py --web-refresh --no-push", workflow)
        cron_values = [
            line.split('"')[1]
            for line in workflow.splitlines()
            if line.strip().startswith("- cron:")
        ]
        self.assertTrue(cron_values)
        self.assertTrue(all(len(value.split()) == 5 for value in cron_values))

    def test_schedule_gate_handles_daylight_and_standard_time(self) -> None:
        self.assertEqual(
            mb.scheduled_slot(datetime(2026, 7, 27, 13, 10, tzinfo=timezone.utc)),
            "morning",
        )
        self.assertIsNone(
            mb.scheduled_slot(datetime(2026, 7, 27, 14, 10, tzinfo=timezone.utc))
        )
        self.assertEqual(
            mb.scheduled_slot(datetime(2026, 1, 12, 14, 10, tzinfo=timezone.utc)),
            "morning",
        )
        self.assertEqual(
            mb.scheduled_slot(datetime(2026, 7, 27, 20, 45, tzinfo=timezone.utc)),
            "close",
        )
        self.assertEqual(
            mb.scheduled_slot(datetime(2026, 1, 12, 21, 45, tzinfo=timezone.utc)),
            "close",
        )

    def test_web_refresh_gate_allows_continuous_silent_updates(self) -> None:
        self.assertTrue(
            mb.web_refresh_allowed(datetime(2026, 7, 27, 3, 0, tzinfo=timezone.utc))
        )
        self.assertTrue(
            mb.web_refresh_allowed(datetime(2026, 7, 27, 23, 59, tzinfo=timezone.utc))
        )
        self.assertTrue(
            mb.web_refresh_allowed(datetime(2026, 7, 26, 4, 0, tzinfo=timezone.utc))
        )
        self.assertFalse(
            mb.web_refresh_allowed(datetime(2026, 7, 26, 23, 0))
        )

    def test_weekend_refresh_does_not_enable_phone_notification_slots(self) -> None:
        weekend = datetime(2026, 7, 26, 13, 10, tzinfo=timezone.utc)
        self.assertIsNone(mb.scheduled_slot(weekend))
        self.assertEqual(mb._brief_mode(weekend), "周末新闻")

    def test_weekday_mode_distinguishes_premarket_from_regular_session(self) -> None:
        self.assertEqual(
            mb._brief_mode(datetime(2026, 7, 27, 13, 10, tzinfo=timezone.utc)),
            "盘前简报",
        )
        self.assertEqual(
            mb._brief_mode(datetime(2026, 7, 27, 13, 30, tzinfo=timezone.utc)),
            "晨间简报",
        )


class RenderTests(unittest.TestCase):
    def test_demo_render_contains_market_facts_and_responsive_css(self) -> None:
        now = datetime(2026, 7, 27, 21, 0, tzinfo=timezone.utc)
        pulse, assessment, world = mb._demo_payload(now)
        assessment.articles[0]["headline"] = "Market falls <script>alert(1)</script>"
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            output = mb.render_html(pulse, assessment, world, output_dir, now)
            page = output.read_text(encoding="utf-8")
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
        self.assertIn("S&amp;P 500", page)
        self.assertIn("-1.19%", page)
        self.assertIn("今日总览", page)
        self.assertIn("偏利空", page)
        self.assertIn("板块核心新闻", page)
        self.assertIn("半导体", page)
        self.assertIn("SPCX / SpaceX 相关", page)
        self.assertIn("MRVL · Marvell", page)
        self.assertIn("后台目标每 15 分钟更新", page)
        self.assertIn("本页每分钟检查新版本", page)
        self.assertIn("后台数据已延迟", page)
        self.assertIn('fetch("status.json?ts=" + Date.now()', page)
        self.assertIn("window.setInterval(() =>", page)
        self.assertEqual(status["generated_at"], now.isoformat())
        self.assertEqual(status["overview"], "偏利空")
        self.assertEqual(status["refresh_policy"]["backend_target_minutes"], 15)
        self.assertIn("@media (max-width: 680px)", page)
        self.assertIn("--bg: #ffffff", page)
        self.assertIn('<meta name="theme-color" content="#ffffff">', page)
        self.assertNotIn("black-translucent", page)
        self.assertIn('<details class="story-details">', page)
        self.assertIn("查看摘要与原文", page)
        self.assertIn('<p class="original-headline">原文：Market falls</p>', page)
        self.assertIn('<details class="sector-block">', page)
        self.assertIn('<details class="special-block">', page)
        self.assertNotIn("<script>alert(1)</script>", page)

    def test_premarket_render_keeps_extended_hours_separate_from_prior_close(self) -> None:
        now = datetime(2026, 7, 27, 13, 0, tzinfo=timezone.utc)
        pulse, assessment, world = mb._demo_payload(now)
        premarket = mb._demo_premarket(pulse, now)
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            output = mb.render_html(
                pulse,
                assessment,
                world,
                output_dir,
                now,
                premarket=premarket,
            )
            page = output.read_text(encoding="utf-8")
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))

        self.assertIn("盘前简报", page)
        self.assertLess(page.index("本周关键事件"), page.index("PREMARKET · DELAYED"))
        self.assertIn("PREMARKET · DELAYED", page)
        self.assertIn("盘前行情", page)
        self.assertIn("相对昨收", page)
        self.assertIn("成交细节", page)
        self.assertIn('<details class="mini-details">', page)
        self.assertIn("数据说明", page)
        self.assertIn("成交量为 4:00 AM ET 起盘前累计", page)
        self.assertIn("最近完整交易日 · Friday, July 24, 2026", page)
        self.assertEqual(status["mode"], "盘前简报")
        self.assertTrue(status["premarket"]["active"])
        self.assertEqual(status["premarket"]["feed"], "sip")
        self.assertIn("SPY", status["premarket"]["moves"])


if __name__ == "__main__":
    unittest.main()
