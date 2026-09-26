"""Voice API contract, provider failures, limits, and credential isolation."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from voice import VoiceService, NoRedirect, MAX_AUDIO
from demo_workspace import WorkspaceError

class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=Path(self.tmp.name)/'voice.env'
        self.env.write_text('ELEVENLABS_API_KEY=test-only-credential\nELEVENLABS_VOICE_ID=stockVoice\n')
        self.voice=VoiceService(self.env)
        self.voice.opener=Mock()
        self.environment=patch.dict(os.environ,{'ELEVENLABS_API_KEY':'','ELEVENLABS_VOICE_ID':''})
        self.environment.start()
    def tearDown(self):
        self.environment.stop();self.tmp.cleanup()
    def response(self,data):
        self.voice.opener.open.return_value.__enter__.return_value.read.return_value=data
    def test_key_is_server_only_and_speech_body_is_correct(self):
        self.response(b'ID3-audio')
        self.assertEqual(self.voice.speak('Read my notes.'),b'ID3-audio')
        request=self.voice.opener.open.call_args.args[0]
        self.assertEqual(request.get_header('Xi-api-key'),'test-only-credential')
        self.assertEqual(request.full_url,'https://api.elevenlabs.io/v1/text-to-speech/stockVoice?output_format=mp3_44100_128')
        self.assertEqual(json.loads(request.data)['model_id'],'eleven_multilingual_v2')
        self.assertNotIn('test-only-credential',json.dumps(self.voice.status()))
    def test_transcription_multipart_and_output(self):
        self.response(json.dumps({'text':'Clara runs the money.'}).encode())
        self.assertEqual(self.voice.transcribe(b'encoded-audio','audio/webm;codecs=opus')['text'],'Clara runs the money.')
        req=self.voice.opener.open.call_args.args[0]
        self.assertIn(b'name="model_id"\r\n\r\nscribe_v2',req.data)
        self.assertIn(b'filename="debrief.webm"',req.data)
        self.assertIn(b'encoded-audio',req.data)
        self.assertEqual(req.full_url,'https://api.elevenlabs.io/v1/speech-to-text')
    def test_missing_key_fails_without_network(self):
        self.env.write_text('')
        self.assertFalse(self.voice.status()['enabled'])
        with self.assertRaises(WorkspaceError):self.voice.speak('Hello')
        self.voice.opener.open.assert_not_called()
    def test_invalid_inputs_do_not_call_provider(self):
        for text in ['',None,'x'*6001]:
            with self.assertRaises(WorkspaceError):self.voice.speak(text)
        for audio,mime in [(b'','audio/webm'),(b'x','text/html'),(b'x'*(MAX_AUDIO+1),'audio/webm')]:
            with self.assertRaises(WorkspaceError):self.voice.transcribe(audio,mime)
        self.voice.opener.open.assert_not_called()
    def test_provider_errors_never_echo_details_or_key(self):
        for code in [401,403,402,422,429,500]:
            self.voice.opener.open.side_effect=urllib.error.HTTPError('https://api.elevenlabs.io',code,'secret raw error',{},io.BytesIO(b'test-only-credential'))
            with self.assertRaises(WorkspaceError) as error:self.voice.speak('Hello')
            self.assertNotIn('credential',str(error.exception))
            self.assertNotIn('secret',str(error.exception))
    def test_network_failure_is_actionable(self):
        self.voice.opener.open.side_effect=urllib.error.URLError('sensitive request details')
        with self.assertRaises(WorkspaceError) as error:self.voice.speak('Hello')
        self.assertIn('internet connection',str(error.exception))
    def test_transcript_failures_are_explicit(self):
        for response in [b'not json',b'[]',b'{"text":""}',json.dumps({'text':'x'*6001}).encode()]:
            self.response(response)
            with self.assertRaises(WorkspaceError):self.voice.transcribe(b'audio','audio/mp4')
    def test_redirect_cannot_forward_auth(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://other.example'))
    def test_voice_id_cannot_override_host_or_path(self):
        self.env.write_text('ELEVENLABS_API_KEY=test-only-credential\nELEVENLABS_VOICE_ID=../../other\n')
        with self.assertRaises(WorkspaceError):self.voice.speak('Hello')
        self.voice.opener.open.assert_not_called()

class VoiceRouteTests(unittest.TestCase):
    def setUp(self):
        import server
        from types import SimpleNamespace
        self.handler=server.Handler.__new__(server.Handler)
        self.handler.app=SimpleNamespace(workspace=SimpleNamespace(token='test-token'),voice=Mock())
        self.handler.client_address=('127.0.0.1',50000)
        self.handler.send_json=lambda status,body,*args:setattr(self,'response',(status,body))
        self.handler._send=lambda status,body,mime,*args:setattr(self,'response',(status,body,mime))
    def call(self,path,raw,content_type,token='test-token'):
        self.handler.path='/api/workspace/voice/'+path
        self.handler.headers={'Host':'127.0.0.1:8877','Origin':'http://127.0.0.1:8877',
                              'X-Workspace-Token':token,'Content-Type':content_type,'Content-Length':str(len(raw))}
        self.handler.rfile=io.BytesIO(raw)
        self.handler.do_POST();return self.response
    def test_binary_transcribe_and_speak_routes(self):
        self.handler.app.voice.transcribe.return_value={'text':'Hello'}
        self.assertEqual(self.call('transcribe',b'abc','audio/webm'),(200,{'text':'Hello'}))
        self.handler.app.voice.speak.return_value=b'ID3'
        self.assertEqual(self.call('speak',b'{"text":"hello"}','application/json'),(200,b'ID3','audio/mpeg'))
    def test_token_protects_both_routes(self):
        for path,raw,mime in [('transcribe',b'abc','audio/webm'),('speak',b'{"text":"hello"}','application/json')]:
            self.assertEqual(self.call(path,raw,mime,token='')[0],403)
        self.handler.app.voice.speak.assert_not_called()
        self.handler.app.voice.transcribe.assert_not_called()
