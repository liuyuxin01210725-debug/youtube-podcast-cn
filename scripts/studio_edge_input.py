"""The Studio Edge input boundary and its text-free render evidence."""

import hashlib
import json
import re


EDGE_REQUEST_SOURCE = 'edge-tts:Communicate.tts_config/mkssml'
EDGE_INPUT_VERSION = 1
EDGE_SILENCE = {'mode': 'silence', 'codepoints': 0}
SILENCE_FRAMES = 2880
SILENCE_RATE = 24000
_TURN_MARKER = re.compile(r"(?<!\S)>>+\s*")
_STAGE = re.compile(
    r"[\[【〔(（]"
    r"(?:音乐|背景音乐|笑声?|掌声|叹气|清嗓子?|咳嗽|鼓掌|沉默|停顿|吸气|呼气|听不清|"
    r"music|laughter|laughs?|applause|gasps?|sighs?|coughs?|snorts?|clears throat|inaudible|crosstalk|beat)"
    r"[\]】〕)）]",
    re.IGNORECASE,
)
_SPEAKABLE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_SHORT_VOICE = re.compile(r"([a-z]{2,})-([A-Z]{2,})-([A-Za-z0-9-]+Neural)")
_WIRE_VOICE = re.compile(
    r"Microsoft Server Speech Text to Speech Voice \(([a-z]{2,}-[A-Z]{2,}(?:-[A-Za-z0-9]+)?), "
    r"([A-Za-z0-9-]+Neural)\)"
)
_REQUEST_FIELDS = {'voice', 'voice_argument', 'language', 'speed', 'ssml_language', 'sdk_version', 'source'}
_INPUT_FIELDS = {'mode', 'voice_slot', 'text_sha256', 'codepoints', 'request', 'request_sha256'}


def clean_tts_text(text):
    """Apply the existing Studio cleanup before passing text to Edge."""
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _TURN_MARKER.sub("", cleaned)
    cleaned = _STAGE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def speakable_tts(text):
    return _SPEAKABLE.search(text or "") is not None


def observe_edge_text(text):
    """Count Unicode code points of the application string passed to Communicate."""
    return {'codepoints': len(text), 'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}


def edge_voice_language(voice):
    match = _WIRE_VOICE.fullmatch(voice or '') if isinstance(voice, str) else None
    return match[1] if match else None


def edge_request_sha256(request):
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _edge_wire_voice(argument):
    """Match the pinned SDK's short-name normalization for evidence comparison."""
    match = _SHORT_VOICE.fullmatch(argument)
    if not match:
        return argument
    language, region, name = match.groups()
    if '-' in name:
        region_suffix, name = name.split('-', 1)
        region += '-' + region_suffix
    return f'Microsoft Server Speech Text to Speech Voice ({language}-{region}, {name})'


def valid_edge_input(evidence, spoken, voice_slot):
    """Check one observed input against the document without consulting current defaults."""
    if not isinstance(evidence, dict):
        return False
    if not speakable_tts(spoken):
        return evidence == EDGE_SILENCE and type(evidence['codepoints']) is int
    request = evidence.get('request')
    fields = set(evidence)
    if 'request_id' in fields and (not isinstance(evidence['request_id'], str)
                                   or not re.fullmatch(r'[0-9a-f]{32}', evidence['request_id'])):
        return False
    if (fields - {'request_id'} != _INPUT_FIELDS or evidence.get('mode') != 'request'
            or evidence.get('voice_slot') != voice_slot or type(evidence.get('codepoints')) is not int
            or not isinstance(request, dict) or set(request) != _REQUEST_FIELDS):
        return False
    if any(evidence.get(key) != value for key, value in observe_edge_text(spoken).items()):
        return False
    voice = request.get('voice')
    language = edge_voice_language(voice)
    argument = request.get('voice_argument')
    return (language is not None and request.get('language') == language
            and isinstance(argument, str) and _edge_wire_voice(argument) == voice
            and request.get('source') == EDGE_REQUEST_SOURCE
            and all(isinstance(request.get(key), str) and re.fullmatch(pattern, request[key])
                    for key, pattern in (
                        ('speed', r'[+-][0-9]+%'),
                        ('ssml_language', r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*'),
                        ('sdk_version', r'[0-9]+(?:\.[0-9]+)+'),
                    ))
            and evidence['request_sha256'] == edge_request_sha256(request))
