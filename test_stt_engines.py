"""Unit tests for the transcription engine chain (Groq -> local -> OpenAI).

Every engine is mocked - no network, no model load. The switches under
test are GROQ_API_KEY, GROQ_STT_MODEL, STT_ENGINE and STT_LOCAL (see the
chain docs at the top of sync_pipeline.py).
"""

import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import sync_pipeline


SEGS = [{"start": 0.0, "end": 1.5, "text": "Hello there world"}]
WORDS = [{"start": 0.0, "end": 0.5}, {"start": 0.5, "end": 1.0},
         {"start": 1.0, "end": 1.5}]


class _FakeResult:
    """Stands in for the OpenAI-shaped verbose_json response object."""

    def __init__(self, segments=None, words=None):
        self.segments = segments
        self.words = words


class _GranularityError(Exception):
    pass


class _FakeClient:
    """Records create() calls; returns queued results or raises queued errors."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.audio = types.SimpleNamespace(
            transcriptions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0) if self.script else None
        if isinstance(item, Exception):
            raise item
        return item


class EngineTestBase(unittest.TestCase):
    """Env isolation: no engine is configured unless a test says otherwise."""

    VARS = ("GROQ_API_KEY", "OPENAI_API_KEY", "WHISPER_API_KEY",
            "STT_ENGINE", "STT_LOCAL", "GROQ_STT_MODEL")

    def setUp(self):
        self._saved = {v: os.environ.get(v) for v in self.VARS}
        for var in self.VARS:
            os.environ.pop(var, None)

        def _restore():
            for var, val in self._saved.items():
                if val is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = val

        self.addCleanup(_restore)

    def fake(self, func_name, result=None, error=None, recorder=None):
        """Patch one engine function, appending its name to `recorder`."""
        def _call(_audio_path):
            if recorder is not None:
                recorder.append(func_name)
            if error is not None:
                raise error
            return result
        return mock.patch.object(sync_pipeline, func_name, _call)


class TestEngineOrder(EngineTestBase):
    def test_default_order_is_groq_first(self):
        self.assertEqual(
            sync_pipeline._engine_order(), ["groq", "local", "openai"]
        )

    def test_stt_engine_pins_one(self):
        os.environ["STT_ENGINE"] = "groq"
        self.assertEqual(sync_pipeline._engine_order(), ["groq"])

    def test_unknown_stt_engine_raises(self):
        os.environ["STT_ENGINE"] = "nope"
        with self.assertRaises(RuntimeError):
            sync_pipeline._engine_order()

    def test_stt_local_zero_drops_local(self):
        os.environ["STT_LOCAL"] = "0"
        os.environ["GROQ_API_KEY"] = "gsk_test"
        self.assertEqual(sync_pipeline._engine_order(), ["groq", "openai"])

    def test_groq_client_none_without_key(self):
        self.assertIsNone(sync_pipeline._get_groq_client())

    def test_groq_client_none_with_blank_key(self):
        os.environ["GROQ_API_KEY"] = "   "
        self.assertIsNone(sync_pipeline._get_groq_client())


class TestChain(EngineTestBase):
    def test_groq_first_when_key_set(self):
        os.environ["GROQ_API_KEY"] = "gsk_test"
        calls = []
        with mock.patch.object(sync_pipeline, "_get_groq_client", lambda: object()), \
             self.fake("_transcribe_groq", (SEGS, WORDS), recorder=calls), \
             self.fake("_transcribe_local", error=AssertionError("local ran"),
                       recorder=calls):
            engine, segments, words = sync_pipeline.transcribe_with_engine(
                Path("x.mp3")
            )
        self.assertEqual(engine, "groq")
        self.assertEqual(segments, SEGS)
        self.assertEqual(words, WORDS)
        self.assertEqual(calls, ["_transcribe_groq"])

    def test_skips_groq_without_key_and_uses_local(self):
        calls = []
        with self.fake("_transcribe_local", (SEGS, WORDS), recorder=calls):
            engine, _s, _w = sync_pipeline.transcribe_with_engine(Path("x.mp3"))
        self.assertEqual(engine, "local")
        self.assertEqual(calls, ["_transcribe_local"])

    def test_groq_failure_falls_through_to_local(self):
        os.environ["GROQ_API_KEY"] = "gsk_test"
        with mock.patch.object(sync_pipeline, "_get_groq_client", lambda: object()), \
             self.fake("_transcribe_groq", error=RuntimeError("429 rate limited")), \
             self.fake("_transcribe_local", (SEGS, WORDS)):
            engine, _s, _w = sync_pipeline.transcribe_with_engine(Path("x.mp3"))
        self.assertEqual(engine, "local")

    def test_engine_failure_logs_console_and_stats(self):
        # A fallback must never be silent: the console line explains a
        # "why whisper instead of groq?" support question, and the stats
        # event keeps the answer after the console is gone.
        os.environ["GROQ_API_KEY"] = "gsk_test"
        logged = []
        with mock.patch.object(sync_pipeline, "_get_groq_client",
                               lambda: object()), \
             self.fake("_transcribe_groq",
                       error=RuntimeError("429 rate limited")), \
             self.fake("_transcribe_local", (SEGS, WORDS)), \
             mock.patch.object(sync_pipeline, "_log_transcribe_event",
                               lambda event, detail: logged.append(
                                   (event, detail))), \
             mock.patch("builtins.print") as printer:
            engine, _s, _w = sync_pipeline.transcribe_with_engine(
                Path("x.mp3")
            )
        self.assertEqual(engine, "local")
        printed = " ".join(
            " ".join(str(a) for a in call.args)
            for call in printer.call_args_list
        )
        self.assertIn("groq failed", printed)
        self.assertIn("429 rate limited", printed)
        self.assertIn(("engine_fallback", "groq: 429 rate limited"), logged)

    def test_openai_is_last(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        with self.fake("_transcribe_local", error=ImportError("no faster_whisper")), \
             self.fake("_transcribe_api", (SEGS, WORDS)):
            engine, _s, _w = sync_pipeline.transcribe_with_engine(Path("x.mp3"))
        self.assertEqual(engine, "openai")

    def test_all_engines_fail_raises_with_guidance(self):
        with self.fake("_transcribe_local", error=ImportError("no faster_whisper")):
            with self.assertRaises(RuntimeError) as ctx:
                sync_pipeline.transcribe_with_engine(Path("x.mp3"))
        self.assertIn("GROQ_API_KEY", str(ctx.exception))

    def test_silent_audio_resolves_instead_of_failing(self):
        # Behaviour carried over from the local-only chain: a run that
        # succeeds with no speech resolves the job rather than erroring.
        with self.fake("_transcribe_local", ([], [])):
            engine, segments, _w = sync_pipeline.transcribe_with_engine(
                Path("x.mp3")
            )
        self.assertEqual(engine, "local")
        self.assertEqual(segments, [])

    def test_transcribe_wrapper_omits_engine(self):
        with self.fake("_transcribe_local", (SEGS, WORDS)):
            segments, words = sync_pipeline.transcribe(Path("x.mp3"))
        self.assertEqual(segments, SEGS)
        self.assertEqual(words, WORDS)


class TestJobEngineLabel(EngineTestBase):
    """The manifest's "via ..." label must describe the CURRENT run."""

    def test_new_run_clears_previous_engine(self):
        # A re-transcription starts with no engine: keeping the old label
        # would show "via groq" while the new run may use another engine.
        manifest = {"audio": "voice.wav", "status": "ready",
                    "transcript_engine": "groq"}
        saved = []
        with mock.patch.object(sync_pipeline, "load_manifest",
                               return_value=manifest), \
             mock.patch.object(sync_pipeline, "save_manifest",
                               lambda job_id, m: saved.append(dict(m))), \
             mock.patch.object(sync_pipeline, "job_dir",
                               return_value=Path(tempfile.gettempdir())), \
             mock.patch.object(sync_pipeline.threading, "Thread",
                               mock.Mock()):
            sync_pipeline.start_transcription_job("job_x")
        self.assertEqual(saved[0]["status"], "processing")
        self.assertNotIn("transcript_engine", saved[0])


class TestLocalEngineSelection(EngineTestBase):
    """Which engines this deployment uses, and which one it pre-loads."""

    def test_local_kept_when_no_api_configured(self):
        self.assertEqual(
            sync_pipeline._engine_order(), ["groq", "local", "openai"]
        )
        self.assertTrue(sync_pipeline._should_warm_local())

    def test_local_stays_in_chain_as_last_resort(self):
        # A Groq deployment keeps faster-whisper available (lazy-loaded only
        # if an API call actually fails) - it just never pre-loads it.
        os.environ["GROQ_API_KEY"] = "gsk_test"
        self.assertEqual(
            sync_pipeline._engine_order(), ["groq", "local", "openai"]
        )
        self.assertFalse(sync_pipeline._should_warm_local())

    def test_stt_local_zero_drops_local(self):
        os.environ["STT_LOCAL"] = "0"
        os.environ["GROQ_API_KEY"] = "gsk_test"
        self.assertEqual(sync_pipeline._engine_order(), ["groq", "openai"])
        self.assertFalse(sync_pipeline._should_warm_local())

    def test_local_pinned_wins_over_api(self):
        os.environ["STT_ENGINE"] = "local"
        os.environ["GROQ_API_KEY"] = "gsk_test"
        self.assertEqual(sync_pipeline._engine_order(), ["local"])
        self.assertTrue(sync_pipeline._should_warm_local())

    def test_openai_key_also_counts_as_api(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        self.assertFalse(sync_pipeline._should_warm_local())
        self.assertEqual(
            sync_pipeline._engine_order(), ["groq", "local", "openai"]
        )

    def test_failsafe_keeps_local_when_no_api_at_all(self):
        # STT_LOCAL=0 with no API key would leave nothing to transcribe
        # with and fail every job - local is used anyway.
        os.environ["STT_LOCAL"] = "0"
        self.assertEqual(sync_pipeline._engine_order(), ["local"])
        self.assertTrue(sync_pipeline._should_warm_local())


class TestWarmWhisper(EngineTestBase):
    """The warm-up must not download/load a model the chain will not use."""

    def _warm_calls(self, env):
        for key, val in env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        calls = []
        with mock.patch.object(
            sync_pipeline, "_get_whisper_model", lambda: calls.append(1)
        ):
            sync_pipeline.warm_whisper()
        time.sleep(0.05)        # the warm-up runs in a daemon thread
        return calls

    def test_no_warmup_when_groq_configured(self):
        # The regression: a Groq deployment was downloading ~75MB of
        # faster-whisper weights on every cold start.
        self.assertEqual(self._warm_calls({"GROQ_API_KEY": "gsk_test"}), [])

    def test_no_warmup_when_local_disabled(self):
        calls = self._warm_calls(
            {"STT_LOCAL": "0", "GROQ_API_KEY": "gsk_test"}
        )
        self.assertEqual(calls, [])

    def test_warmup_runs_for_local_only_deployment(self):
        self.assertEqual(len(self._warm_calls({})), 1)

    def test_warmup_runs_when_local_pinned(self):
        calls = self._warm_calls(
            {"STT_ENGINE": "local", "GROQ_API_KEY": "gsk_test"}
        )
        self.assertEqual(len(calls), 1)


class TestGroqEngine(EngineTestBase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.audio = Path(tmp.name) / "voice.wav"
        self.audio.write_bytes(b"fake audio bytes")

    def _client(self, script):
        client = _FakeClient(script)
        patcher = mock.patch.object(
            sync_pipeline, "_get_groq_client", lambda: client
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def test_default_model_and_word_granularity(self):
        client = self._client([_FakeResult(SEGS, WORDS)])
        segments, words = sync_pipeline._transcribe_groq(self.audio)
        self.assertEqual(
            client.calls[0]["model"], sync_pipeline.GROQ_DEFAULT_MODEL
        )
        self.assertEqual(
            client.calls[0]["timestamp_granularities"], ["word", "segment"]
        )
        self.assertEqual(segments, SEGS)
        self.assertEqual(words, WORDS)

    def test_granularity_error_retries_segments_only(self):
        client = self._client([
            _GranularityError(
                "timestamp_granularities=['word'] is not supported"
            ),
            _FakeResult(SEGS, None),
        ])
        segments, words = sync_pipeline._transcribe_groq(self.audio)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            client.calls[1]["timestamp_granularities"], ["segment"]
        )
        self.assertEqual(segments, SEGS)
        self.assertTrue(words, "word timings must be synthesized")
        self.assertEqual(words[0]["start"], 0.0)
        self.assertEqual(words[-1]["end"], 1.5)

    def test_other_errors_are_not_retried(self):
        client = self._client([RuntimeError("500 server error")])
        with self.assertRaises(RuntimeError):
            sync_pipeline._transcribe_groq(self.audio)
        self.assertEqual(len(client.calls), 1)

    def test_empty_transcript_raises(self):
        self._client([_FakeResult([], None)])
        with self.assertRaises(RuntimeError):
            sync_pipeline._transcribe_groq(self.audio)

    def test_model_env_override(self):
        os.environ["GROQ_STT_MODEL"] = "whisper-large-v3"
        client = self._client([_FakeResult(SEGS, WORDS)])
        sync_pipeline._transcribe_groq(self.audio)
        self.assertEqual(client.calls[0]["model"], "whisper-large-v3")

    def test_is_granularity_error(self):
        self.assertTrue(sync_pipeline._is_granularity_error(
            _GranularityError("timestamp_granularities not supported")
        ))
        self.assertFalse(
            sync_pipeline._is_granularity_error(RuntimeError("429"))
        )


class TestApiParser(unittest.TestCase):
    def test_dict_shaped(self):
        result = types.SimpleNamespace(
            segments=[
                {"start": 1.0, "end": 2.0, "text": " hi "},
                {"start": 2, "end": 3, "text": "   "},
            ],
            words=[{"start": 1.0, "end": 1.5, "word": "hi"}],
        )
        segments, words = sync_pipeline._parse_api_transcription(result)
        self.assertEqual(
            segments, [{"start": 1.0, "end": 2.0, "text": "hi"}]
        )
        self.assertEqual(words, [{"start": 1.0, "end": 1.5}])

    def test_object_shaped(self):
        seg = types.SimpleNamespace(start=0.0, end=1.0, text="hello")
        word = types.SimpleNamespace(start=0.0, end=0.5, word="hello")
        segments, words = sync_pipeline._parse_api_transcription(
            types.SimpleNamespace(segments=[seg], words=[word])
        )
        self.assertEqual(
            segments, [{"start": 0.0, "end": 1.0, "text": "hello"}]
        )
        self.assertEqual(words, [{"start": 0.0, "end": 0.5}])

    def test_missing_fields_are_skipped(self):
        result = types.SimpleNamespace(
            segments=[{"text": "no times"}], words=[{"start": None}]
        )
        segments, words = sync_pipeline._parse_api_transcription(result)
        self.assertEqual((segments, words), ([], []))

    def test_none_fields(self):
        segments, words = sync_pipeline._parse_api_transcription(
            types.SimpleNamespace(segments=None, words=None)
        )
        self.assertEqual((segments, words), ([], []))


if __name__ == "__main__":
    unittest.main()


