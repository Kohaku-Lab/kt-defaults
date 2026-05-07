"""Tests for kt-biome's OTelMetricsPlugin.

All OpenTelemetry imports are mocked — no real OTEL packages required.
Uses a controllable ``Clock`` stub for deterministic timing behaviour.
"""

import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ── Bootstrap: inject mock kohakuterrarium so the plugin can import ──

_mock_kt = ModuleType("kohakuterrarium")
_mock_kt_modules: dict[str, ModuleType] = {}


def _ensure_mod(dotted: str) -> ModuleType:
    parts = dotted.split(".")
    for i in range(len(parts)):
        partial = ".".join(parts[: i + 1])
        if partial not in sys.modules:
            m = ModuleType(partial)
            sys.modules[partial] = m
        if partial not in _mock_kt_modules:
            _mock_kt_modules[partial] = sys.modules[partial]
    return sys.modules[dotted]


# BasePlugin and PluginContext — minimal stubs
class _BasePlugin:
    name: str = ""
    priority: int = 0

    def __init__(self, options=None):
        pass


base_mod = _ensure_mod("kohakuterrarium.modules.plugin.base")
base_mod.BasePlugin = _BasePlugin
base_mod.PluginContext = SimpleNamespace

plugin_pkg = _ensure_mod("kohakuterrarium.modules.plugin")
plugin_pkg.BasePlugin = _BasePlugin

# kohakuterrarium.utils.logging
logging_mod = _ensure_mod("kohakuterrarium.utils.logging")
logging_mod.get_logger = lambda *a, **kw: MagicMock()

# Ensure top-level kohakuterrarium and all needed subpackages
_ensure_mod("kohakuterrarium.modules")
_ensure_mod("kohakuterrarium.utils")
_ensure_mod("kohakuterrarium.session")
_ensure_mod("kohakuterrarium")

# Suppress the real opentelemetry so the plugin's try/except sets
# _otel_available = False cleanly (it already is False in this env).
for _blocked in [
    "opentelemetry",
    "opentelemetry.exporter",
    "opentelemetry.exporter.otlp",
    "opentelemetry.exporter.otlp.proto",
    "opentelemetry.exporter.otlp.proto.http",
    "opentelemetry.exporter.otlp.proto.http.metric_exporter",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.sdk",
    "opentelemetry.sdk.metrics",
    "opentelemetry.sdk.metrics.export",
    "opentelemetry.sdk.resources",
    "opentelemetry.sdk.trace",
    "opentelemetry.sdk.trace.export",
    "opentelemetry.trace",
    "opentelemetry.trace.status",
]:
    if _blocked not in sys.modules:
        sys.modules[_blocked] = ModuleType(_blocked)

# NOW import the module under test
from kt_biome.plugins import otel_metrics as mod
from kt_biome.plugins.otel_metrics import OTelMetricsPlugin


# ── Helpers ──────────────────────────────────────────────────────────


class Clock:
    """Mutable monotonic-clock stub."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _ctx(agent_name: str = "test-agent", session_id: str = "sess-abc123"):
    """Minimal PluginContext stand-in."""
    return SimpleNamespace(agent_name=agent_name, session_id=session_id)


def _make_plugin(options: dict | None = None) -> OTelMetricsPlugin:
    """Create plugin with mocked OTEL instruments pre-populated."""
    opts = dict(options or {})
    opts.setdefault("collector_config_path", str(Path(tempfile.mkdtemp()) / "otel-collector.yaml"))
    plugin = OTelMetricsPlugin(opts)
    plugin._ctx = _ctx()
    for name, _ in mod._COUNTER_DEFS:
        plugin._counters[name] = MagicMock()
    for name, _, _ in mod._HISTOGRAM_DEFS:
        plugin._histograms[name] = MagicMock()
    return plugin


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graceful_no_otel() -> None:
    """1. Plugin is a no-op when OTEL packages are not available."""
    plugin = OTelMetricsPlugin({"collector_config_path": str(Path(tempfile.mkdtemp()) / "otel-collector.yaml")})
    await plugin.on_load(_ctx("lonely"))
    assert plugin._agent_name == "lonely"

    msgs = ["hello"]
    await plugin.post_llm_call(msgs, None, {"prompt_tokens": 100}, model="gpt-5.4")

    assert plugin._counters == {}
    assert plugin._histograms == {}


@pytest.mark.asyncio
async def test_on_load_creates_instruments() -> None:
    """2. on_load creates all 16 counters and 7 histograms via the meter."""
    mock_meter = MagicMock()
    mock_provider = MagicMock()
    mock_provider.get_meter.return_value = mock_meter

    mock_resource_cls = MagicMock()
    mock_exporter_cls = MagicMock()
    mock_reader_cls = MagicMock()
    mock_meter_provider_cls = MagicMock(return_value=mock_provider)

    p_avail = patch("kt_biome.plugins.otel_metrics._otel_available", True)
    p_resource = patch("kt_biome.plugins.otel_metrics.Resource", mock_resource_cls, create=True)
    p_exporter = patch("kt_biome.plugins.otel_metrics.OTLPMetricExporter", mock_exporter_cls, create=True)
    p_reader = patch("kt_biome.plugins.otel_metrics.PeriodicExportingMetricReader", mock_reader_cls, create=True)
    p_provider = patch("kt_biome.plugins.otel_metrics.MeterProvider", mock_meter_provider_cls, create=True)

    with p_avail, p_resource, p_exporter, p_reader, p_provider:
        plugin = OTelMetricsPlugin({"endpoint": "http://otel:4318/v1/metrics", "collector_config_path": str(Path(tempfile.mkdtemp()) / "otel-collector.yaml")})
        await plugin.on_load(_ctx())

    mock_meter.create_counter.assert_called()
    mock_meter.create_histogram.assert_called()
    assert mock_meter.create_counter.call_count == len(mod._COUNTER_DEFS)
    assert mock_meter.create_histogram.call_count == len(mod._HISTOGRAM_DEFS)


@pytest.mark.asyncio
async def test_on_unload_shuts_down_provider() -> None:
    """3. on_unload force-flushes and shuts down the provider."""
    plugin = _make_plugin()
    mock_provider = MagicMock()
    plugin._provider = mock_provider

    await plugin.on_unload()
    mock_provider.force_flush.assert_called_once()
    mock_provider.shutdown.assert_called_once()
    assert plugin._provider is None
    assert plugin._meter is None


@pytest.mark.asyncio
async def test_llm_call_timing_and_tokens() -> None:
    """4. pre→post LLM call records duration and token counters."""
    plugin = _make_plugin()
    clock = Clock()
    msgs = ["msg1"]

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_llm_call(msgs, model="gpt-5.4")
        clock.advance(0.5)
        await plugin.post_llm_call(
            msgs, None, {"prompt_tokens": 100, "completion_tokens": 50},
            model="gpt-5.4",
        )

    plugin._histograms["kt.llm.duration"].record.assert_called_once_with(
        500.0, {"model": "gpt-5.4", "request_source": "main", "session_id": "sess-abc123"}
    )
    plugin._counters["kt.llm.calls"].add.assert_called_with(1, {"model": "gpt-5.4", "request_source": "main", "session_id": "sess-abc123"})
    plugin._counters["kt.llm.tokens.prompt"].add.assert_called_with(
        100, {"model": "gpt-5.4", "request_source": "main", "session_id": "sess-abc123"}
    )
    plugin._counters["kt.llm.tokens.completion"].add.assert_called_with(
        50, {"model": "gpt-5.4", "request_source": "main", "session_id": "sess-abc123"}
    )


@pytest.mark.asyncio
async def test_llm_call_usage_none() -> None:
    """5. post_llm_call with usage=None does not crash."""
    plugin = _make_plugin()
    msgs = ["msg1"]

    await plugin.pre_llm_call(msgs, model="test-model")
    await plugin.post_llm_call(msgs, None, None, model="test-model")

    plugin._counters["kt.llm.calls"].add.assert_called_with(1, {"model": "test-model", "request_source": "main", "session_id": "sess-abc123"})


@pytest.mark.asyncio
async def test_concurrent_llm_calls() -> None:
    """6. Two overlapping LLM calls tracked by different id(messages)."""
    plugin = _make_plugin()
    clock = Clock()
    msgs_a = ["a"]
    msgs_b = ["b"]

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_llm_call(msgs_a, model="m1")
        clock.advance(0.1)
        await plugin.pre_llm_call(msgs_b, model="m2")
        clock.advance(0.4)
        await plugin.post_llm_call(msgs_a, None, {}, model="m1")
        clock.advance(0.1)
        await plugin.post_llm_call(msgs_b, None, {}, model="m2")

    assert plugin._counters["kt.llm.calls"].add.call_count == 2
    assert plugin._start_times == {}


@pytest.mark.asyncio
async def test_tool_dispatch_counter() -> None:
    """7. pre_tool_dispatch increments the dispatches counter."""
    plugin = _make_plugin()
    call = SimpleNamespace(name="bash", args={}, raw="")
    await plugin.pre_tool_dispatch(call, _ctx())

    plugin._counters["kt.tool.dispatches"].add.assert_called_with(
        1, {"tool_name": "bash", "session_id": "sess-abc123"}
    )


@pytest.mark.asyncio
async def test_tool_execute_timing_and_errors() -> None:
    """8. pre→post tool execute records duration; failure bumps error counter."""
    plugin = _make_plugin()
    clock = Clock()

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_tool_execute({"cmd": "ls"}, tool_name="bash", job_id="j1")
        clock.advance(0.3)
        result_ok = SimpleNamespace(success=True)
        await plugin.post_tool_execute(result_ok, tool_name="bash", job_id="j1")

    args, kwargs = plugin._histograms["kt.tool.duration"].record.call_args
    assert args[0] == pytest.approx(300.0)
    assert args[1] == {"tool_name": "bash", "session_id": "sess-abc123"}
    plugin._counters["kt.tool.calls"].add.assert_called_with(1, {"tool_name": "bash", "session_id": "sess-abc123"})
    plugin._counters["kt.tool.errors"].add.assert_not_called()

    # Failure path
    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_tool_execute({"cmd": "bad"}, tool_name="bash", job_id="j2")
        clock.advance(0.2)
        result_fail = SimpleNamespace(success=False)
        await plugin.post_tool_execute(result_fail, tool_name="bash", job_id="j2")

    plugin._counters["kt.tool.errors"].add.assert_called_with(1, {"tool_name": "bash", "session_id": "sess-abc123"})


@pytest.mark.asyncio
async def test_tool_result_none() -> None:
    """9. post_tool_execute with result=None treats as success."""
    plugin = _make_plugin()
    await plugin.pre_tool_execute({"cmd": "ls"}, tool_name="bash", job_id="j3")
    await plugin.post_tool_execute(None, tool_name="bash", job_id="j3")

    plugin._counters["kt.tool.calls"].add.assert_called_with(1, {"tool_name": "bash", "session_id": "sess-abc123"})
    plugin._counters["kt.tool.errors"].add.assert_not_called()


@pytest.mark.asyncio
async def test_subagent_run() -> None:
    """10. pre→post subagent run records duration, turns, and error on failure."""
    plugin = _make_plugin()
    clock = Clock()

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_subagent_run(
            "do something", name="worker", job_id="j4"
        )
        clock.advance(1.0)
        result = SimpleNamespace(success=False, turns=7)
        await plugin.post_subagent_run(result, name="worker", job_id="j4")

    plugin._counters["kt.subagent.runs"].add.assert_called_with(1, {"subagent_name": "worker", "request_source": "subagent", "session_id": "sess-abc123"})
    plugin._histograms["kt.subagent.duration"].record.assert_called_with(
        1000.0, {"subagent_name": "worker", "request_source": "subagent", "session_id": "sess-abc123"}
    )
    plugin._histograms["kt.subagent.turns"].record.assert_called_with(
        7, {"subagent_name": "worker", "request_source": "subagent", "session_id": "sess-abc123"}
    )
    plugin._counters["kt.subagent.errors"].add.assert_called_with(1, {"subagent_name": "worker", "request_source": "subagent", "session_id": "sess-abc123"})


@pytest.mark.asyncio
async def test_compact_hooks() -> None:
    """11. on_compact_start/end increment counters and observe histogram."""
    plugin = _make_plugin()

    await plugin.on_compact_start(context_length=5000)
    plugin._counters["kt.compact.count"].add.assert_called_with(1, {"session_id": "sess-abc123"})
    plugin._histograms["kt.compact.context_length"].record.assert_called_with(
        5000, {"session_id": "sess-abc123"}
    )

    await plugin.on_compact_end(summary="compressed", messages_removed=12)
    plugin._histograms["kt.compact.messages_removed"].record.assert_called_with(
        12, {"session_id": "sess-abc123"}
    )


@pytest.mark.asyncio
async def test_on_event() -> None:
    """12. on_event increments the events counter with event type."""
    plugin = _make_plugin()

    event = SimpleNamespace(type="tool_output")
    await plugin.on_event(event)
    plugin._counters["kt.events"].add.assert_called_with(
        1, {"event_type": "tool_output", "session_id": "sess-abc123"}
    )

    # event=None defaults to "unknown"
    await plugin.on_event(None)
    plugin._counters["kt.events"].add.assert_called_with(1, {"event_type": "unknown", "session_id": "sess-abc123"})


@pytest.mark.asyncio
async def test_on_interrupt() -> None:
    """13. on_interrupt increments the interrupts counter."""
    plugin = _make_plugin()

    await plugin.on_interrupt()
    plugin._counters["kt.interrupts"].add.assert_called_with(1, {"session_id": "sess-abc123"})


@pytest.mark.asyncio
async def test_session_duration() -> None:
    """14. on_load sets session start; on_agent_stop records duration."""
    plugin = _make_plugin()
    clock = Clock(1000.0)

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.on_load(_ctx("session-test"))
        clock.advance(42.0)
        await plugin.on_agent_stop()

    plugin._histograms["kt.agent.session.duration"].record.assert_called_with(
        42.0, {"agent": "session-test"}
    )
    plugin._counters["kt.agent.stops"].add.assert_called_with(1, {"agent": "session-test"})


@pytest.mark.asyncio
async def test_on_agent_start_increments_starts() -> None:
    plugin = _make_plugin()
    plugin._agent_name = "test-agent"
    await plugin.on_agent_start()
    plugin._counters["kt.agent.starts"].add.assert_called_with(1, {"agent": "test-agent"})


@pytest.mark.asyncio
async def test_on_agent_stop_increments_stops() -> None:
    plugin = _make_plugin()
    plugin._agent_name = "test-agent"
    plugin._session_start = 100.0
    await plugin.on_agent_stop()
    plugin._counters["kt.agent.stops"].add.assert_called_with(1, {"agent": "test-agent"})


def test_metric_names_immutable() -> None:
    """15. All 14 counter names and 7 histogram names exist in the module defs."""
    counter_names = [name for name, _ in mod._COUNTER_DEFS]
    histogram_names = [name for name, _, _ in mod._HISTOGRAM_DEFS]

    assert len(counter_names) == 16
    assert len(histogram_names) == 7

    expected_counters = {
        "kt.llm.calls",
        "kt.llm.tokens.prompt",
        "kt.llm.tokens.completion",
        "kt.llm.tokens.cache_read",
        "kt.llm.tokens.cache_creation",
        "kt.llm.active_time",
        "kt.tool.calls",
        "kt.tool.dispatches",
        "kt.tool.errors",
        "kt.subagent.runs",
        "kt.subagent.errors",
        "kt.compact.count",
        "kt.agent.starts",
        "kt.agent.stops",
        "kt.events",
        "kt.interrupts",
    }
    expected_histograms = {
        "kt.llm.duration",
        "kt.tool.duration",
        "kt.subagent.duration",
        "kt.subagent.turns",
        "kt.compact.context_length",
        "kt.compact.messages_removed",
        "kt.agent.session.duration",
    }

    assert set(counter_names) == expected_counters
    assert set(histogram_names) == expected_histograms


# ── New tests for Claude Code alignment (P0) ──────────────────────────


@pytest.mark.asyncio
async def test_llm_tokens_cache_read_and_creation_split() -> None:
    """16. post_llm_call emits cache_read from cached_tokens and cache_creation from cache_write_tokens."""
    plugin = _make_plugin()
    msgs = ["msg"]

    await plugin.post_llm_call(
        msgs, None,
        {"prompt_tokens": 100, "completion_tokens": 50, "cached_tokens": 80, "cache_write_tokens": 30},
        model="gpt-5.4",
    )

    plugin._counters["kt.llm.tokens.cache_read"].add.assert_called_with(
        80, {"model": "gpt-5.4", "request_source": "main", "session_id": "sess-abc123"}
    )
    plugin._counters["kt.llm.tokens.cache_creation"].add.assert_called_with(
        30, {"model": "gpt-5.4", "request_source": "main", "session_id": "sess-abc123"}
    )


@pytest.mark.asyncio
async def test_llm_no_legacy_cached_counter() -> None:
    """17. kt.llm.tokens.cached no longer exists in counter defs."""
    counter_names = {name for name, _ in mod._COUNTER_DEFS}
    assert "kt.llm.tokens.cached" not in counter_names
    assert "kt.llm.tokens.cache_read" in counter_names
    assert "kt.llm.tokens.cache_creation" in counter_names


@pytest.mark.asyncio
async def test_llm_attrs_include_request_source_main() -> None:
    """18. post_llm_call sets request_source='main' on all LLM metrics."""
    plugin = _make_plugin()
    msgs = ["m"]
    clock = Clock()

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_llm_call(msgs, model="m")
        clock.advance(0.1)
        await plugin.post_llm_call(msgs, None, {"prompt_tokens": 10}, model="m")

    expected_attrs = {"model": "m", "request_source": "main", "session_id": "sess-abc123"}
    plugin._counters["kt.llm.calls"].add.assert_called_with(1, expected_attrs)


@pytest.mark.asyncio
async def test_llm_attrs_include_session_id() -> None:
    """19. post_llm_call includes session_id from PluginContext."""
    plugin = _make_plugin()
    plugin._ctx = _ctx(session_id="my-session-42")
    msgs = ["m"]

    await plugin.post_llm_call(msgs, None, {"prompt_tokens": 5}, model="x")

    for call in plugin._counters["kt.llm.calls"].add.call_args_list:
        _, kwargs = call
        assert kwargs.get("session_id") == "my-session-42" or call[0][1].get("session_id") == "my-session-42"


@pytest.mark.asyncio
async def test_subagent_attrs_include_request_source_and_session() -> None:
    """20. post_subagent_run sets request_source='subagent' and session_id."""
    plugin = _make_plugin()
    clock = Clock()

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_subagent_run("task", name="worker", job_id="j10")
        clock.advance(2.0)
        result = SimpleNamespace(success=True, turns=3)
        await plugin.post_subagent_run(result, name="worker", job_id="j10")

    expected_attrs = {"subagent_name": "worker", "request_source": "subagent", "session_id": "sess-abc123"}
    plugin._counters["kt.subagent.runs"].add.assert_called_with(1, expected_attrs)
    plugin._histograms["kt.subagent.duration"].record.assert_called_with(2000.0, expected_attrs)
    plugin._histograms["kt.subagent.turns"].record.assert_called_with(3, expected_attrs)


@pytest.mark.asyncio
async def test_llm_active_time_accumulates() -> None:
    """21. kt.llm.active_time counter accumulates LLM call duration in seconds."""
    plugin = _make_plugin()
    clock = Clock()

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_llm_call(["a"], model="m")
        clock.advance(0.5)
        await plugin.post_llm_call(["a"], None, {"prompt_tokens": 10}, model="m")

        await plugin.pre_llm_call(["b"], model="m")
        clock.advance(1.5)
        await plugin.post_llm_call(["b"], None, {"prompt_tokens": 20}, model="m")

    calls = plugin._counters["kt.llm.active_time"].add.call_args_list
    assert len(calls) == 2
    assert calls[0][0][0] == pytest.approx(0.5)
    assert calls[1][0][0] == pytest.approx(1.5)


def test_counter_defs_now_16() -> None:
    """22. Counter defs total 16 (cache_read/creation split + active_time + starts/stops)."""
    assert len(mod._COUNTER_DEFS) == 16


# ── Trace span tests (TDD — failing until tracer implemented) ─────────


class SpanRecorder:
    """Collects all spans created during a test for assertion."""

    def __init__(self) -> None:
        self.spans: list[SimpleNamespace] = []

    def start_span(self, name: str, attributes: dict | None = None, **kw: Any) -> SimpleNamespace:
        span = SimpleNamespace(
            name=name,
            attributes=dict(attributes or {}),
            status=None,
            ended=False,
            _recorder=self,
        )
        span.set_attribute = lambda key, value: span.attributes.__setitem__(key, value)
        span.set_status = lambda status: setattr(span, "status", status)
        span.end = lambda: setattr(span, "ended", True)
        self.spans.append(span)
        return span


def _make_tracer_plugin(options: dict | None = None) -> tuple[OTelMetricsPlugin, SpanRecorder]:
    """Create plugin with mocked OTEL instruments AND a tracer that records spans."""
    plugin = _make_plugin(options)
    recorder = SpanRecorder()

    mock_tracer = MagicMock()
    mock_tracer.start_span = recorder.start_span
    mock_tracer.start_as_current_span = lambda name, **kw: MagicMock(
        __enter__=lambda s: recorder.start_span(name, **kw),
        __exit__=lambda s, *exc: None,
    )

    plugin._tracer = mock_tracer

    mock_status = SimpleNamespace(OK="OK", ERROR="ERROR")
    mod.StatusCode = mock_status

    return plugin, recorder


@pytest.mark.asyncio
async def test_llm_call_emits_span() -> None:
    """23. pre→post LLM call emits a kt.llm.call span with model, tokens, and request_source."""
    plugin, recorder = _make_tracer_plugin()
    clock = Clock()
    msgs = ["msg"]

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_llm_call(msgs, model="deepseek-v4")
        clock.advance(0.3)
        await plugin.post_llm_call(
            msgs, None,
            {"prompt_tokens": 100, "completion_tokens": 50, "cached_tokens": 20, "cache_write_tokens": 5},
            model="deepseek-v4",
        )

    spans = [s for s in recorder.spans if s.name == "kt.llm.call"]
    assert len(spans) == 1
    s = spans[0]
    assert s.attributes.get("model") == "deepseek-v4"
    assert s.attributes.get("request_source") == "main"
    assert s.attributes.get("session_id") == "sess-abc123"
    assert s.attributes.get("llm.prompt_tokens") == 100
    assert s.attributes.get("llm.completion_tokens") == 50
    assert s.attributes.get("llm.cache_read_tokens") == 20
    assert s.attributes.get("llm.cache_creation_tokens") == 5
    assert s.ended


@pytest.mark.asyncio
async def test_tool_execute_emits_span() -> None:
    """24. pre→post tool execute emits a kt.tool.execute span with tool_name and success."""
    plugin, recorder = _make_tracer_plugin()
    clock = Clock()

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_tool_execute({"cmd": "ls"}, tool_name="bash", job_id="j1")
        clock.advance(0.2)
        result = SimpleNamespace(success=True)
        await plugin.post_tool_execute(result, tool_name="bash", job_id="j1")

    spans = [s for s in recorder.spans if s.name == "kt.tool.execute"]
    assert len(spans) == 1
    s = spans[0]
    assert s.attributes.get("tool_name") == "bash"
    assert s.attributes.get("success") is True
    assert s.ended


@pytest.mark.asyncio
async def test_subagent_run_emits_span() -> None:
    """25. pre→post subagent run emits a kt.subagent.run span with name and turns."""
    plugin, recorder = _make_tracer_plugin()
    clock = Clock()

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_subagent_run("do stuff", name="worker", job_id="j2")
        clock.advance(1.5)
        result = SimpleNamespace(success=True, turns=4)
        await plugin.post_subagent_run(result, name="worker", job_id="j2")

    spans = [s for s in recorder.spans if s.name == "kt.subagent.run"]
    assert len(spans) == 1
    s = spans[0]
    assert s.attributes.get("subagent_name") == "worker"
    assert s.attributes.get("request_source") == "subagent"
    assert s.attributes.get("session_id") == "sess-abc123"
    assert s.attributes.get("success") is True
    assert s.attributes.get("turns") == 4
    assert s.ended


@pytest.mark.asyncio
async def test_tool_failure_span_status_error() -> None:
    """26. Failed tool execution sets error status and success=False on span."""
    plugin, recorder = _make_tracer_plugin()
    clock = Clock()

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_tool_execute({"cmd": "bad"}, tool_name="bash", job_id="j3")
        clock.advance(0.1)
        result = SimpleNamespace(success=False)
        await plugin.post_tool_execute(result, tool_name="bash", job_id="j3")

    spans = [s for s in recorder.spans if s.name == "kt.tool.execute"]
    assert len(spans) == 1
    assert spans[0].attributes.get("success") is False
    assert spans[0].status is not None


@pytest.mark.asyncio
async def test_on_load_creates_tracer_when_otel_available() -> None:
    """27. on_load creates a tracer alongside the meter when OTEL is available."""
    mock_meter = MagicMock()
    mock_provider = MagicMock()
    mock_provider.get_meter.return_value = mock_meter

    mock_tracer = MagicMock()
    mock_tracer_provider = MagicMock()
    mock_tracer_provider.get_tracer.return_value = mock_tracer

    p_avail = patch("kt_biome.plugins.otel_metrics._otel_available", True)
    p_trace = patch("kt_biome.plugins.otel_metrics._trace_available", True)
    p_resource = patch("kt_biome.plugins.otel_metrics.Resource", MagicMock(), create=True)
    p_exporter = patch("kt_biome.plugins.otel_metrics.OTLPMetricExporter", MagicMock(), create=True)
    p_reader = patch("kt_biome.plugins.otel_metrics.PeriodicExportingMetricReader", MagicMock(), create=True)
    p_provider = patch("kt_biome.plugins.otel_metrics.MeterProvider", MagicMock(return_value=mock_provider), create=True)
    p_span_exporter = patch("kt_biome.plugins.otel_metrics.OTLPSpanExporter", MagicMock(), create=True)
    p_span_processor = patch("kt_biome.plugins.otel_metrics.BatchSpanProcessor", MagicMock(), create=True)
    p_tracer_provider = patch("kt_biome.plugins.otel_metrics.TracerProvider", MagicMock(return_value=mock_tracer_provider), create=True)
    p_trace_api = patch("kt_biome.plugins.otel_metrics.trace_api", MagicMock(), create=True)
    p_status = patch("kt_biome.plugins.otel_metrics.StatusCode", MagicMock(), create=True)

    with p_avail, p_trace, p_resource, p_exporter, p_reader, p_provider, \
         p_span_exporter, p_span_processor, p_tracer_provider, p_trace_api, p_status:
        plugin = OTelMetricsPlugin({"endpoint": "http://otel:4318/v1/metrics", "collector_config_path": str(Path(tempfile.mkdtemp()) / "otel-collector.yaml")})
        await plugin.on_load(_ctx())

    mock_tracer_provider.get_tracer.assert_called_once()
    assert plugin._tracer is not None


# ── session_id 补全 + UUID fallback tests ──────────────────────────────


@pytest.mark.asyncio
async def test_tool_execute_attrs_include_session_id() -> None:
    """28. post_tool_execute includes session_id in all metric attrs."""
    plugin = _make_plugin()
    clock = Clock()

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_tool_execute({"cmd": "ls"}, tool_name="bash", job_id="j1")
        clock.advance(0.3)
        result = SimpleNamespace(success=True)
        await plugin.post_tool_execute(result, tool_name="bash", job_id="j1")

    expected_attrs = {"tool_name": "bash", "session_id": "sess-abc123"}
    plugin._counters["kt.tool.calls"].add.assert_called_with(1, expected_attrs)
    plugin._histograms["kt.tool.duration"].record.assert_called_with(
        pytest.approx(300.0), expected_attrs
    )


@pytest.mark.asyncio
async def test_tool_dispatch_attrs_include_session_id() -> None:
    """29. pre_tool_dispatch includes session_id in attrs."""
    plugin = _make_plugin()
    call = SimpleNamespace(name="bash", args={}, raw="")
    await plugin.pre_tool_dispatch(call, _ctx())

    plugin._counters["kt.tool.dispatches"].add.assert_called_with(
        1, {"tool_name": "bash", "session_id": "sess-abc123"}
    )


@pytest.mark.asyncio
async def test_compact_hooks_include_session_id() -> None:
    """30. on_compact_start/end include session_id in attrs."""
    plugin = _make_plugin()

    await plugin.on_compact_start(context_length=5000)
    plugin._counters["kt.compact.count"].add.assert_called_with(1, {"session_id": "sess-abc123"})
    plugin._histograms["kt.compact.context_length"].record.assert_called_with(
        5000, {"session_id": "sess-abc123"}
    )

    await plugin.on_compact_end(summary="compressed", messages_removed=12)
    plugin._histograms["kt.compact.messages_removed"].record.assert_called_with(
        12, {"session_id": "sess-abc123"}
    )


@pytest.mark.asyncio
async def test_on_event_includes_session_id() -> None:
    """31. on_event includes session_id in attrs."""
    plugin = _make_plugin()
    event = SimpleNamespace(type="tool_output")
    await plugin.on_event(event)
    plugin._counters["kt.events"].add.assert_called_with(
        1, {"event_type": "tool_output", "session_id": "sess-abc123"}
    )


@pytest.mark.asyncio
async def test_on_interrupt_includes_session_id() -> None:
    """32. on_interrupt includes session_id in attrs."""
    plugin = _make_plugin()
    await plugin.on_interrupt()
    plugin._counters["kt.interrupts"].add.assert_called_with(1, {"session_id": "sess-abc123"})


@pytest.mark.asyncio
async def test_uuid_fallback_when_session_id_empty() -> None:
    """33. When session_id is empty, a UUID is auto-generated and stable across calls."""
    plugin = _make_plugin()
    plugin._ctx = _ctx(session_id="")

    await plugin.on_interrupt()
    await plugin.on_interrupt()

    calls = plugin._counters["kt.interrupts"].add.call_args_list
    assert len(calls) == 2
    sid1 = calls[0][0][1]["session_id"]
    sid2 = calls[1][0][1]["session_id"]
    assert sid1 != ""
    assert sid1 == sid2
    assert len(sid1) == 32  # uuid4 hex format


@pytest.mark.asyncio
async def test_uuid_not_used_when_session_id_present() -> None:
    """34. When session_id is provided, it is used as-is (no UUID fallback)."""
    plugin = _make_plugin()
    plugin._ctx = _ctx(session_id="my-real-session")

    await plugin.on_interrupt()
    calls = plugin._counters["kt.interrupts"].add.call_args_list
    assert calls[0][0][1]["session_id"] == "my-real-session"


# ── Collector config template tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_collector_config_created_on_first_load() -> None:
    """35. on_load creates collector config template at configured path if absent."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "otel-collector.yaml"
        plugin = OTelMetricsPlugin({"collector_config_path": str(config_path)})
        assert not config_path.exists()

        await plugin.on_load(_ctx("config-test"))

        assert config_path.exists()
        content = config_path.read_text()
        assert "receivers:" in content
        assert "otlp:" in content
        assert "exporters:" in content
        assert "prometheus:" in content


@pytest.mark.asyncio
async def test_collector_config_not_overwritten_on_reload() -> None:
    """36. on_load does not overwrite an existing collector config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "otel-collector.yaml"
        config_path.write_text("my-custom-config: true\n")

        plugin = OTelMetricsPlugin({"collector_config_path": str(config_path)})
        await plugin.on_load(_ctx())

        assert config_path.read_text() == "my-custom-config: true\n"


@pytest.mark.asyncio
async def test_collector_config_default_path() -> None:
    """37. Default collector config path is ~/.kohakuterrarium/otel-collector.yaml."""
    plugin = OTelMetricsPlugin()
    expected = Path.home() / ".kohakuterrarium" / "otel-collector.yaml"
    assert plugin._collector_config_path == expected


@pytest.mark.asyncio
async def test_collector_config_custom_path() -> None:
    """38. collector_config_path can be overridden via options."""
    plugin = OTelMetricsPlugin({"collector_config_path": "/tmp/my-otel.yaml"})
    assert plugin._collector_config_path == Path("/tmp/my-otel.yaml")
