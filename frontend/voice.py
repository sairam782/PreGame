"""Server-side ElevenLabs adapter. The key and recordings are never persisted by this module."""
from __future__ import annotations
import json
import os
import re
import socket
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from demo_workspace import WorkspaceError

API_ROOT = 'https://api.elevenlabs.io/v1'
MAX_AUDIO = 8 * 1024 * 1024
AUDIO_TYPES = {'audio/webm':'webm', 'audio/mp4':'m4a', 'audio/ogg':'ogg', 'audio/wav':'wav', 'audio/mpeg':'mp3'}


def configuration(path):
    values = {}
    try:
        for line in Path(path).read_text().splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and not key.startswith('#'):
                values[key] = value.strip().strip('\"\'')
    except FileNotFoundError:
        pass
    return {key:os.environ.get(key) or values.get(key, default) for key,default in {
        'ELEVENLABS_API_KEY':'',
        'ELEVENLABS_VOICE_ID':'JBFqnCBsd6RMkjVDRZzb',
    }.items()}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the API key to a redirected host.


class VoiceService:
    def __init__(self, config_path):
        self.config_path = Path(config_path)
        self.opener = urllib.request.build_opener(NoRedirect())

    def status(self):
        return {'enabled':bool(configuration(self.config_path)['ELEVENLABS_API_KEY']), 'provider':'ElevenLabs',
                'max_recording_seconds':90, 'max_audio_bytes':MAX_AUDIO}

    def _request(self, path, body, content_type, accept, max_size):
        key = configuration(self.config_path)['ELEVENLABS_API_KEY']
        if not key:
            raise WorkspaceError(503, 'Voice is not configured on this laptop. Set ELEVENLABS_API_KEY in frontend/voice.env.')
        req = urllib.request.Request(API_ROOT + path, data=body, headers={
            'xi-api-key':key, 'Content-Type':content_type, 'Accept':accept}, method='POST')
        try:
            with self.opener.open(req, timeout=60) as response:
                data = response.read(max_size + 1)
                if len(data) > max_size:
                    raise WorkspaceError(502, 'The voice response was too large. Try a shorter passage.')
                return data
        except urllib.error.HTTPError as exc:
            # Never echo the provider body, request headers, or exception text.
            errors = {401:'ElevenLabs rejected the API key.', 403:'The API key does not have permission for this voice operation.',
                      402:'ElevenLabs credits are unavailable.', 429:'ElevenLabs is busy or the account limit has been reached. Try again shortly.',
                      422:'ElevenLabs could not process this recording or text. Try again with a shorter input.'}
            message = errors.get(exc.code, 'The voice service could not complete this request. Try again.')
            exc.close()
            raise WorkspaceError(502, message) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            raise WorkspaceError(503, 'Cannot reach ElevenLabs. Voice needs an internet connection; you can still type and review notes.') from None

    def transcribe(self, audio, content_type):
        mime = content_type.split(';')[0].strip().lower()
        if mime not in AUDIO_TYPES:
            raise WorkspaceError(415, 'This recording format is not supported. Try Chrome or Edge.')
        if not audio or len(audio) > MAX_AUDIO:
            raise WorkspaceError(413, 'Record up to 90 seconds (maximum 8 MB).')
        boundary = 'pregame-' + uuid.uuid4().hex
        fields = {'model_id':'scribe_v2', 'tag_audio_events':'false', 'diarize':'false'}
        parts = []
        for name,value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="debrief.{AUDIO_TYPES[mime]}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
        parts.extend([audio, f'\r\n--{boundary}--\r\n'.encode()])
        raw = self._request('/speech-to-text', b''.join(parts), f'multipart/form-data; boundary={boundary}', 'application/json', 2*1024*1024)
        try:
            text = json.loads(raw).get('text')
        except (ValueError, AttributeError):
            raise WorkspaceError(502, 'The voice service returned an unreadable transcript.') from None
        if not isinstance(text,str) or not text.strip():
            raise WorkspaceError(422, 'No speech was detected. Try recording again.')
        if len(text) > 6000:
            raise WorkspaceError(422, 'The transcript is too long for a debrief. Record a shorter summary.')
        return {'text':text.strip()}

    def speak(self, text):
        if not isinstance(text,str) or not text.strip() or len(text) > 6000:
            raise WorkspaceError(400, 'Read-back text must contain 1–6,000 characters.')
        voice_id = configuration(self.config_path)['ELEVENLABS_VOICE_ID']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',voice_id):
            raise WorkspaceError(503, 'The configured voice ID is invalid.')
        return self._request('/text-to-speech/' + voice_id + '?output_format=mp3_44100_128',
                             json.dumps({'text':text.strip(),'model_id':'eleven_multilingual_v2'}).encode(),
                             'application/json', 'audio/mpeg', 16*1024*1024)
