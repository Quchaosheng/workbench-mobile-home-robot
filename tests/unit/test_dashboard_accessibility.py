"""Issue #171: the read-only robot monitoring view.

The markup assertions pin the accessible structure. The behavioural assertions
execute the real `app.js` under Node, because a view that fabricates a healthy
card from a missing metric would satisfy any string check.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "apps" / "dashboard"
SCRIPT = DASHBOARD / "app.js"
NODE = shutil.which("node")

# The DOM bootstrap is the only part of app.js that needs a browser, so the
# module is truncated there and its pure functions are exported for Node.
BOOTSTRAP = "\nasync function initialize("
EXPORT_TAIL = (
    "\nmodule.exports = { monitoringViewState, monitoringCardsFor, monitoringAlertsFor, "
    "monitoringTrendRows, monitoringMetricText, monitoringMetricStatus, monitoringAlertSignature, "
    "monitoringAlertStatusByMetric, monitoringRetryDelay, monitoringUnitLabel, "
    "monitoringDomainLabel, monitoringAlertSummaryText, renderMonitoringAlerts, renderMonitoringCards, "
    "renderMonitoringTrend };\n"
)
MODULE_PRELUDE = "const module = { exports: {} };\n(function (module, exports) {\n"
MODULE_EPILOGUE = "\n})(module, module.exports);\n"

# The render functions write into `document.getElementById(...)`. The stub keeps
# them executable under Node so a test can assert on the markup they produce
# rather than on the source that produces it.
DOM_STUB = (
    "const __elements = {};\n"
    'const __element = () => ({ innerHTML: "", hidden: false, textContent: "", '
    'className: "", classList: { toggle() {}, add() {}, remove() {} } });\n'
    "const document = { getElementById: (id) => (__elements[id] = __elements[id] || __element()) };\n"
    "globalThis.__dom = __elements;\n"
)


# A minimal structural parser for the two layout assertions. Counting substrings
# cannot tell a panel that is nested inside a column from one that is merely
# near it, and the layout bug was exactly a mis-nested panel.
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Node:
    __slots__ = ("children", "classes", "tag")

    def __init__(self, tag: str, classes: set[str]) -> None:
        self.tag = tag
        self.classes = classes
        self.children: list[_Node] = []


class _Root(_Node):
    def __init__(self) -> None:
        super().__init__("root", set())


def _parse_html(markup: str) -> _Root:
    root = _Root()
    stack: list[_Node] = [root]
    for token in re.finditer(r"<(?P<closing>/)?(?P<tag>[a-zA-Z][a-zA-Z0-9]*)(?P<attrs>[^>]*?)(?P<self>/?)>", markup):
        tag = token.group("tag").lower()
        if token.group("closing"):
            if len(stack) > 1 and stack[-1].tag == tag:
                stack.pop()
            continue
        classes = set(re.findall(r'class="([^"]*)"', token.group("attrs")))
        classes = {name for group in classes for name in group.split()}
        node = _Node(tag, classes)
        stack[-1].children.append(node)
        if tag not in _VOID_TAGS and not token.group("self"):
            stack.append(node)
    return root


def _find(markup: str, *classes: str) -> _Node:
    wanted = set(classes)
    matches = [node for node in _walk(_parse_html(markup)) if wanted <= node.classes]
    assert len(matches) == 1, (classes, len(matches))
    return matches[0]


def _walk(node: _Node):
    for child in node.children:
        yield child
        yield from _walk(child)


def _direct_children(markup: str, *classes: str) -> list[_Node]:
    return _find(markup, *classes).children


def _descendants(node: _Node) -> list[_Node]:
    return list(_walk(node))


def _run_node(expression: str, payload: object) -> object:
    """Evaluate one monitoring helper in Node with a JSON argument."""
    source = SCRIPT.read_text(encoding="utf-8")
    pure = source[: source.index(BOOTSTRAP)] + EXPORT_TAIL
    program = "".join(
        [
            MODULE_PRELUDE,
            DOM_STUB,
            pure,
            MODULE_EPILOGUE,
            f"process.stdout.write(JSON.stringify({expression}));",
        ]
    )
    completed = subprocess.run(
        [NODE, "-e", program, json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"node failed: {completed.stderr}")
    return json.loads(completed.stdout)


def health_payload(**overrides: object) -> dict:
    """A minimal validated health payload with one metric per domain."""
    payload = {
        "read_only": True,
        "status": "healthy",
        "source": "health.jsonl",
        "current": {
            "collected_at": 1000.0,
            "clock_id": "monotonic",
            "overall": "healthy",
            "domains": {
                "safety": {
                    "status": "healthy",
                    "metrics": [
                        {
                            "name": "safety.estop_channels_ok",
                            "value": True,
                            "unit": "bool",
                            "expected_source": "safety_mcu",
                            "source": "safety_mcu",
                            "observed_at": 999.99,
                            "age_s": 0.01,
                            "state": "fresh",
                            "source_status": "available",
                            "missing": False,
                            "stale": False,
                        }
                    ],
                }
            },
        },
        "alerts": {"summary": {"total": 0}, "active": []},
    }
    payload.update(overrides)
    return payload


def metric(**overrides: object) -> dict:
    base = {
        "name": "safety.estop_channels_ok",
        "value": True,
        "unit": "bool",
        "expected_source": "safety_mcu",
        "source": "safety_mcu",
        "observed_at": 999.99,
        "age_s": 0.01,
        "state": "fresh",
        "source_status": "available",
        "missing": False,
        "stale": False,
    }
    base.update(overrides)
    return base


def test_dashboard_exposes_filter_tab_replay_and_live_region_state() -> None:
    markup = (DASHBOARD / "index.html").read_text(encoding="utf-8")

    assert markup.count('class="filter-button') == 3
    assert markup.count('aria-pressed="false"') >= 3
    assert 'aria-controls="overview-view"' in markup
    assert 'aria-controls="replay-view"' in markup
    assert 'id="replay-tab"' in markup and 'tabindex="-1"' in markup
    assert 'aria-label="任务摘要" aria-live="polite" aria-atomic="true"' in markup
    assert 'id="attention-banner" class="attention-banner" role="status"' in markup
    assert 'id="replay-play" class="play-button" type="button" aria-pressed="false"' in markup
    assert 'id="replay-position" aria-live="polite"' in markup


def test_dashboard_script_keeps_keyboard_and_dynamic_state_in_sync() -> None:
    script = (DASHBOARD / "app.js").read_text(encoding="utf-8")

    for key in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"):
        assert key in script
    assert 'tab.setAttribute("aria-selected", String(selected))' in script
    assert "tab.tabIndex = selected ? 0 : -1" in script
    assert 'item.setAttribute("aria-pressed", String(selected))' in script
    assert "replayRange.setAttribute" in script
    assert '"aria-valuetext"' in script
    assert 'scrollIntoView({ block: "nearest", inline: "nearest" })' in script


def test_dashboard_supports_reduced_motion_and_forced_colors() -> None:
    styles = (DASHBOARD / "styles.css").read_text(encoding="utf-8")

    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert "animation-duration: 0.01ms !important" in styles
    assert "animation-iteration-count: 1 !important" in styles
    assert "@media (forced-colors: active)" in styles


def test_dashboard_offers_a_third_read_only_monitoring_tab() -> None:
    markup = (DASHBOARD / "index.html").read_text(encoding="utf-8")

    assert markup.count('class="view-tab') == 4
    assert 'id="monitoring-tab"' in markup
    assert 'aria-controls="monitoring-view"' in markup
    assert 'id="monitoring-view" class="view-panel"' in markup
    assert 'role="tabpanel" aria-labelledby="monitoring-tab"' in markup
    assert 'id="monitoring-overall"' in markup
    assert 'id="monitoring-cards"' in markup
    assert 'id="monitoring-alerts"' in markup
    assert 'id="monitoring-trend"' in markup
    # Exactly one polite live region announces alert changes without repeating
    # the whole table on every poll.
    assert 'id="monitoring-live" class="visually-hidden" role="status" aria-live="polite" aria-atomic="true"' in markup
    # The view must not offer any control verb: no acknowledge, reset or stop.
    monitoring = markup.split('id="monitoring-view"', 1)[1].split('id="replay-view"', 1)[0]
    assert "<button" not in monitoring.replace('<button id="evidence-close"', "")


def test_dashboard_marks_monitoring_as_a_simulation_fixture_and_read_only() -> None:
    script = (DASHBOARD / "app.js").read_text(encoding="utf-8")

    assert "\u4eff\u771f\u5939\u5177" in script and "\u672a\u8fde\u63a5\u7269\u7406\u4f20\u611f\u5668" in script
    assert "\u53ea\u8bfb" in script
    assert "monitoringSourceLabel" in script


def test_monitoring_view_is_never_green_without_a_fresh_value() -> None:
    missing = metric(value=None, state="missing", source_status="missing", missing=True, observed_at=None, age_s=None)
    stale = metric(state="stale", stale=True, age_s=42.0)
    conflict = metric(value=None, state="conflict", source_status="conflict")
    degraded = metric(value=False, state="degraded")
    healthy = metric()

    cases = {
        "missing": (missing, "unknown"),
        "stale": (stale, "unknown"),
        "conflict": (conflict, "fault"),
        "degraded": (degraded, "degraded"),
        "healthy": (healthy, "healthy"),
    }
    call = "module.exports.monitoringMetricStatus(JSON.parse(process.argv[1]))"
    for name, (candidate, expected) in cases.items():
        assert _run_node(call, candidate) == expected, name

    # A missing metric renders as a word, never as a fabricated zero or "false".
    text = _run_node("module.exports.monitoringMetricText(JSON.parse(process.argv[1]))", missing)
    assert text == "未上报"
    assert "0" not in text and "false" not in text.lower()

    stale_text = _run_node("module.exports.monitoringMetricText(JSON.parse(process.argv[1]))", stale)
    assert stale_text == "数据陈旧"


def test_a_missing_or_stale_critical_metric_cannot_render_a_healthy_card() -> None:
    missing_metric = metric(
        value=None, state="missing", source_status="missing", missing=True, observed_at=None, age_s=None
    )
    for name, candidate in (("missing", missing_metric), ("stale", metric(state="stale", stale=True, age_s=90.0))):
        payload = health_payload()
        payload["current"]["domains"]["safety"]["metrics"] = [candidate]
        cards = _run_node("module.exports.monitoringCardsFor(JSON.parse(process.argv[1]))", payload)
        safety = next(card for card in cards if card["id"] == "safety")
        assert safety["status"] == "unknown", name
        assert safety["metrics"][0]["status"] == "unknown", name
        assert safety["metrics"][0]["text"] in {"未上报", "数据陈旧"}, name


def test_the_overall_view_reports_unavailable_rather_than_a_stale_status() -> None:
    healthy = _run_node("module.exports.monitoringViewState(JSON.parse(process.argv[1]), null)", health_payload())
    assert healthy["available"] is True and healthy["status"] == "healthy"

    failed = _run_node(
        "module.exports.monitoringViewState(JSON.parse(process.argv[1]), process.argv[2])",
        health_payload(),
    )
    # Passing a failure string clears availability: a stale green card must not
    # survive a failed refresh.
    failed = _run_node(
        'module.exports.monitoringViewState(null, "network down")',
        {},
    )
    assert failed["available"] is False
    assert failed["status"] == "unavailable"
    assert failed["cards"] == [] and failed["alerts"] == []

    absent = _run_node("module.exports.monitoringViewState(null, null)", {})
    assert absent["available"] is False and absent["status"] == "unavailable"


def test_alerts_are_sorted_by_severity_then_age_and_deduplicated_by_identity() -> None:
    payload = health_payload()
    payload["alerts"] = {
        "summary": {"total": 3},
        "active": [
            {
                "alert_id": "can.link_ok:can_link_loss:can",
                "condition": "can_link_loss",
                "severity": "critical",
                "state": "active",
                "metric": "can.link_ok",
                "first_seen_at": 12.0,
                "last_seen_at": 20.0,
                "count": 3,
                "evidence_ref": "alert://can.link_ok:can_link_loss:can",
            },
            {
                "alert_id": "backend.available:backend_unavailable:backend",
                "condition": "backend_unavailable",
                "severity": "warning",
                "state": "active",
                "metric": "backend.available",
                "first_seen_at": 1.0,
                "last_seen_at": 2.0,
                "count": 1,
                "evidence_ref": "alert://backend.available:backend_unavailable:backend",
            },
            {
                "alert_id": "safety.estop_channels_ok:estop_unavailable:safety_mcu",
                "condition": "estop_unavailable",
                "severity": "critical",
                "state": "active",
                "metric": "safety.estop_channels_ok",
                "first_seen_at": 3.0,
                "last_seen_at": 4.0,
                "count": 8,
                "evidence_ref": "alert://safety.estop_channels_ok:estop_unavailable:safety_mcu",
            },
        ],
    }
    ordered = _run_node("module.exports.monitoringAlertsFor(JSON.parse(process.argv[1]))", payload)
    assert [alert["condition"] for alert in ordered] == [
        "estop_unavailable",
        "can_link_loss",
        "backend_unavailable",
    ]

    # The live-region signature changes only when the alert set changes, so a
    # repeated poll does not re-announce the same alerts.
    signature = _run_node("module.exports.monitoringAlertSignature(JSON.parse(process.argv[1]))", ordered)
    assert signature == _run_node("module.exports.monitoringAlertSignature(JSON.parse(process.argv[1]))", ordered)
    assert signature != _run_node("module.exports.monitoringAlertSignature(JSON.parse(process.argv[1]))", ordered[:1])
    assert _run_node("module.exports.monitoringAlertSignature(JSON.parse(process.argv[1]))", []) == ""


def test_the_trend_table_is_bounded_and_keeps_unknown_domains_unknown() -> None:
    snapshots = [
        {"collected_at": float(index), "overall": "unknown", "domains": {"safety": "unknown"}} for index in range(40)
    ]
    rows = _run_node("module.exports.monitoringTrendRows(JSON.parse(process.argv[1]))", {"snapshots": snapshots})
    assert len(rows) == 20
    assert rows[-1]["collected_at"] == 39.0
    assert rows[-1]["overall"] == "unknown"

    # An unreadable overall value must not be silently upgraded to healthy.
    rows = _run_node(
        "module.exports.monitoringTrendRows(JSON.parse(process.argv[1]))",
        {"snapshots": [{"collected_at": 1.0, "overall": "not-a-status", "domains": {}}]},
    )
    assert rows[0]["overall"] == "unknown"


def test_monitoring_polling_pauses_when_the_view_is_hidden() -> None:
    script = (DASHBOARD / "app.js").read_text(encoding="utf-8")

    assert "const MONITORING_REFRESH_MS" in script
    assert "const MONITORING_MAX_BACKOFF_MS" in script
    assert "MONITORING_MAX_BACKOFF_MS" in script.split("monitoringBackoff = Math.min", 1)[1]
    assert "document.hidden" in script and "visibilitychange" in script
    assert "state.monitoringRequest?.abort()" in script
    assert "AbortController" in script
    # The failed-refresh path clears the payload instead of keeping it.
    failure = script.split("state.monitoringFailure = error.message", 1)[0]
    assert "state.monitoring = null" in failure


def test_monitoring_refresh_targets_the_versioned_read_only_endpoints() -> None:
    script = (DASHBOARD / "app.js").read_text(encoding="utf-8")

    assert 'fetch("/api/v1/health"' in script
    assert 'fetch("/api/v1/health/history"' in script
    # No write verb is issued by the monitoring view.
    assert 'method: "POST"' not in script and 'method: "PUT"' not in script and 'method: "DELETE"' not in script


def test_a_fresh_but_alerting_metric_cannot_show_a_healthy_card() -> None:
    # A disk with 0 free bytes is a fresh, valid reading that still violates a
    # configured threshold. The backend alert is authoritative, so the storage
    # card must not render 正常 next to an active warning.
    payload = health_payload()
    payload["status"] = "degraded"
    payload["current"]["domains"] = {
        "compute": {
            "status": "healthy",
            "metrics": [
                metric(name="compute.disk_free_bytes", value=0, unit="bytes", state="fresh"),
            ],
        }
    }
    payload["alerts"] = {
        "summary": {"total": 1},
        "active": [
            {
                "alert_id": "compute.disk_free_bytes:disk_pressure:linux.statvfs",
                "condition": "disk_pressure",
                "severity": "warning",
                "state": "active",
                "metric": "compute.disk_free_bytes",
                "first_seen_at": 10.0,
                "last_seen_at": 12.0,
                "count": 1,
                "evidence_ref": "alert://compute.disk_free_bytes:disk_pressure:linux.statvfs",
            }
        ],
    }
    # The raw metric classification is still "fresh"...
    raw = _run_node(
        "module.exports.monitoringMetricStatus(JSON.parse(process.argv[1]))",
        payload["current"]["domains"]["compute"]["metrics"][0],
    )
    assert raw == "healthy"
    # ...but the card defers to the active alert and reports degraded.
    cards = _run_node("module.exports.monitoringCardsFor(JSON.parse(process.argv[1]))", payload)
    storage = next(card for card in cards if card["id"] == "storage")
    assert storage["status"] == "degraded"
    assert storage["metrics"][0]["status"] == "degraded"

    # A critical alert on a metric raises the card to fault, not merely degraded.
    payload["alerts"]["active"][0]["severity"] = "critical"
    cards = _run_node("module.exports.monitoringCardsFor(JSON.parse(process.argv[1]))", payload)
    storage = next(card for card in cards if card["id"] == "storage")
    assert storage["status"] == "fault"


def test_the_card_status_map_takes_the_most_severe_alert_per_metric() -> None:
    payload = health_payload()
    payload["alerts"] = {
        "active": [
            {"metric": "can.link_ok", "severity": "warning", "condition": "can_bus_off"},
            {"metric": "can.link_ok", "severity": "critical", "condition": "can_link_loss"},
        ]
    }
    mapping = _run_node(
        "Array.from(module.exports.monitoringAlertStatusByMetric(JSON.parse(process.argv[1])).entries())",
        payload,
    )
    assert mapping == [["can.link_ok", "fault"]]


def test_a_failed_refresh_schedules_a_backoff_retry() -> None:
    # A retained payload must not suppress the retry: the delay depends on the
    # derived availability, not on whether the last payload survived.
    unavailable = {"available": False, "status": "unavailable", "cards": [], "alerts": []}
    available = {"available": True, "status": "healthy", "cards": [], "alerts": []}

    assert (
        _run_node("module.exports.monitoringRetryDelay(JSON.parse(process.argv[1]), null, null)", unavailable) == 5000
    )
    assert (
        _run_node("module.exports.monitoringRetryDelay(JSON.parse(process.argv[1]), {stale: true}, 20000)", unavailable)
        == 20000
    )
    # A retained payload with no recorded backoff still schedules the base delay.
    assert (
        _run_node("module.exports.monitoringRetryDelay(JSON.parse(process.argv[1]), {stale: true}, null)", unavailable)
        == 5000
    )
    # An available view schedules nothing; polling is driven by the interval.
    assert _run_node("module.exports.monitoringRetryDelay(JSON.parse(process.argv[1]), {}, null)", available) is None


def test_every_health_domain_has_a_card_that_can_render_its_metrics() -> None:
    # The registry ships one domain per status the operator must be able to read.
    # A domain without a matching card silently dropped its metrics: the
    # `application` domain (readiness, uptime, restarts, queue depth and cycle
    # age) was absent from the card list, so five of 35 published metrics were
    # never rendered and never reported as missing either.
    payload = health_payload()
    payload["current"]["domains"] = {
        "application": {
            "status": "healthy",
            "metrics": [
                metric(name="app.readiness", value=True, unit="bool"),
                metric(name="app.uptime_seconds", value=5.0, unit="seconds"),
                metric(name="app.restart_count", value=5, unit="restarts"),
                metric(name="app.queue_depth", value=5, unit="items"),
                metric(name="app.last_successful_cycle_age_seconds", value=5.0, unit="seconds"),
            ],
        }
    }
    cards = _run_node("module.exports.monitoringCardsFor(JSON.parse(process.argv[1]))", payload)
    application = next((card for card in cards if card["id"] == "application"), None)
    assert application is not None, "no card matches the application domain"
    assert [item["name"] for item in application["metrics"]] == [
        "app.readiness",
        "app.uptime_seconds",
        "app.restart_count",
        "app.queue_depth",
        "app.last_successful_cycle_age_seconds",
    ]

    # No card may match a domain another card already owns, or metrics would be
    # counted twice and one card would contradict the other.
    registry_domains = {
        "application",
        "communication",
        "compute",
        "power",
        "robot",
        "safety",
        "task",
    }
    owned: set[str] = set()
    for domain in sorted(registry_domains):
        names = [f"{domain}.sample_metric"]
        if domain == "compute":
            names = ["compute.disk_free_bytes"]
        if domain == "robot":
            names = ["nav.localization_ok", "motion.controller_ok", "perception.fresh"]
        payload["current"]["domains"] = {
            domain: {"status": "healthy", "metrics": [metric(name=name) for name in names]}
        }
        cards = _run_node("module.exports.monitoringCardsFor(JSON.parse(process.argv[1]))", payload)
        matched = [card["id"] for card in cards if card["metrics"]]
        assert len(matched) == len(set(matched)), (domain, matched)
        owned.update(matched)
    assert "application" in owned


def test_a_page_never_renders_an_alert_list_with_a_mismatched_tag() -> None:
    # The alert panel opened a <ul> and closed a </div>. A browser recovers from
    # that, but the markup is still wrong and no tolerant parser should be the
    # only thing keeping the list well formed.
    payload = {
        "available": True,
        "alerts": [
            {
                "alert_id": "compute.disk_free_bytes:disk_pressure:linux.statvfs",
                "condition": "disk_pressure",
                "severity": "warning",
                "state": "active",
                "metric": "compute.disk_free_bytes",
                "observed_value": 0,
                "unit": "bytes",
                "count": 1,
                "evidence_ref": "alert://compute.disk_free_bytes:disk_pressure:linux.statvfs",
            }
        ],
    }
    markup = _run_node(
        "(module.exports.renderMonitoringAlerts(JSON.parse(process.argv[1])), __dom['monitoring-alerts'].innerHTML)",
        payload,
    )
    assert markup.startswith("\n    <ul")
    assert markup.strip().endswith("</ul>")
    assert "</div>" not in markup
    # One alert is one list item, and the shipped condition vocabulary decides
    # the label rather than the English rule prose.
    assert markup.count("<li ") == 1
    assert "磁盘空间不足" in markup
    assert "The disk is at or below the configured pressure level." not in markup
    # The metric and the evidence reference stay attached to the alert.
    assert "compute.disk_free_bytes" in markup
    assert "alert://compute.disk_free_bytes:disk_pressure:linux.statvfs" in markup


def test_the_trend_headers_are_localised_and_the_two_totals_are_distinguished() -> None:
    # The card labels split `communication` into CAN and `robot` into
    # localisation/motion/perception, so the trend table cannot reuse them; it
    # labels the domains the API reports. The page also carried two readings
    # both called 总体: the alert-aware status at the top and the per-snapshot
    # domain roll-up below. They are different numbers and must not share a
    # heading.
    history = {
        "snapshots": [
            {"collected_at": 1000.0, "overall": "healthy", "domains": {"communication": "healthy", "robot": "fault"}}
        ]
    }
    markup = _run_node(
        "(module.exports.renderMonitoringTrend({ available: true, trend: "
        "module.exports.monitoringTrendRows(JSON.parse(process.argv[1])) }), "
        "__dom['monitoring-trend'].innerHTML)",
        history,
    )
    assert "通信" in markup and "机器人" in markup
    assert '<th scope="col">communication</th>' not in markup
    assert '<th scope="col">robot</th>' not in markup
    # The unresolved domain stays reachable as a tooltip rather than vanishing.
    assert 'title="communication"' in markup
    assert "快照总体" in markup
    assert '<th scope="col">总体</th>' not in markup
    # The caption states the difference instead of leaving two 总体 unlabelled.
    assert "顶部" in markup


def test_the_unit_suffix_is_localised_and_an_unknown_unit_is_preserved() -> None:
    assert _run_node("module.exports.monitoringUnitLabel(JSON.parse(process.argv[1]))", "percent") == "%"
    assert _run_node("module.exports.monitoringUnitLabel(JSON.parse(process.argv[1]))", "celsius") == "°C"
    assert _run_node("module.exports.monitoringUnitLabel(JSON.parse(process.argv[1]))", "seconds") == "秒"
    # An unmapped unit is shown as-is: dropping it would silently change a value.
    assert _run_node("module.exports.monitoringUnitLabel(JSON.parse(process.argv[1]))", "widgets") == "widgets"

    payload = health_payload()
    payload["current"]["domains"] = {
        "compute": {
            "status": "healthy",
            "metrics": [metric(name="compute.disk_free_bytes", value=0, unit="bytes")],
        }
    }
    cards = _run_node("module.exports.monitoringCardsFor(JSON.parse(process.argv[1]))", payload)
    storage = next(card for card in cards if card["id"] == "storage")
    assert storage["metrics"][0]["text"] == "0 B"

    # The value stays exact; only the unit word is translated.
    percent = metric(name="compute.cpu_percent", value=5, unit="percent")
    assert _run_node("module.exports.monitoringMetricText(JSON.parse(process.argv[1]))", percent) == "5 %"


def test_a_rendered_metric_name_keeps_the_full_name_available_when_clipped() -> None:
    # The metric column ellipsises; without a title the clipped name is lost.
    payload = health_payload()
    payload["current"]["domains"] = {
        "safety": {
            "status": "healthy",
            "metrics": [metric(name="safety.contactor_permission")],
        }
    }
    markup = _run_node(
        "(module.exports.renderMonitoringCards({ available: true, cards: "
        "module.exports.monitoringCardsFor(JSON.parse(process.argv[1])) }), "
        "__dom['monitoring-cards'].innerHTML)",
        payload,
    )
    assert 'title="safety.contactor_permission"' in markup


def test_the_overview_grid_holds_exactly_two_columns() -> None:
    # `.dashboard-grid` is a fixed two-column grid with no explicit placement, so
    # a third direct child is auto-placed into a second row and leaves the whole
    # right-hand side of that row blank (448x783 px at 1600 px). Panels belong to
    # one of exactly two column stacks; the nesting is the invariant, so the test
    # parses the markup rather than counting class attributes, which a loose
    # panel beside a column would still satisfy.
    markup = (DASHBOARD / "index.html").read_text(encoding="utf-8")
    grid = _direct_children(markup, "dashboard-grid")

    assert [child.classes for child in grid] == [{"dashboard-column"}, {"dashboard-column"}]

    panels = [entry for column in grid for entry in _descendants(column)]
    panel_classes = [entry for entry in panels if "panel" in entry.classes and entry.tag == "section"]
    assert len(panel_classes) == 4
    named = {name for entry in panel_classes for name in entry.classes}
    for panel_class in ("timeline-panel", "evidence-panel", "safety-panel"):
        assert panel_class in named, panel_class
    # Each column holds exactly two panels, so neither is a bare wrapper.
    for column in grid:
        contained = [entry for entry in _descendants(column) if "panel" in entry.classes and entry.tag == "section"]
        assert len(contained) == 2, [entry.classes for entry in contained]

    assert "side-stack" not in markup
    styles = (DASHBOARD / "styles.css").read_text(encoding="utf-8")
    assert ".dashboard-column {" in styles
    assert "align-content: start" in styles.split(".dashboard-column {", 1)[1].split("}", 1)[0]
    # The responsive rules follow the renamed wrapper instead of a dead class.
    assert ".dashboard-column:last-child" in styles
    assert "side-stack" not in styles
