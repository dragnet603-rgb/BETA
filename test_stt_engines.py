"""Unit tests for the transcription engine chain (Groq -> local -> OpenAI).

Every engine is mocked - no network, no model load. The switches under
test are GROQ_API_KEY, GROQ_STT_MODEL, STT_ENGINE and STT_LOCAL (see the
chain docs at the top of sync_pipeline.py).
"""

import os
import tempfile
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


